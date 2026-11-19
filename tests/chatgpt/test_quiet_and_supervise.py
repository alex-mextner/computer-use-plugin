from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import IO

import pytest

from computerlib.targets.chatgpt.commands.quiet import keep_line
from computerlib.targets.chatgpt.stack import Process
from computerlib.targets.chatgpt.supervise import run_foreground

SELF_SIGTERM = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"


def _quiet(*command: str) -> list[str]:
    return [sys.executable, "-m", "computerlib.cli", "chatgpt", "quiet", "--", *command]


@contextlib.contextmanager
def _quiet_process(script: str) -> Generator[subprocess.Popen[bytes]]:
    """quiet running `script`, both streams piped unbuffered, in its own process group: killed whatever the test did, so a
    sleeping command or descendant never outlives it."""
    process = subprocess.Popen(
        _quiet(sys.executable, "-c", script),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_the_journal_keeps_problems_and_drops_per_request_chatter() -> None:
    assert not keep_line('{"severity":"INFO","message":"request"}\n')
    assert not keep_line("[supergateway] StreamableHttp message: {...tool arguments...}\n")
    assert not keep_line("[supergateway] Child stdout: {...}\n")
    assert not keep_line("[supergateway] listening on 8000\n")
    assert keep_line("[supergateway] fatal: cannot bind port\n")
    assert keep_line('{"severity":"ERROR","message":"boom"}\n')
    assert keep_line("panic: runtime error\n")
    # supergateway logs its error level to stderr, about the backend too: those lines must survive now that stderr is filtered
    assert keep_line("[supergateway] Child stderr: Error: Desktop Commander is not installed\n")
    assert keep_line("[supergateway] Child exited: code=5, signal=null\n")
    # ...but a copy of the backend's reply stays out even when the tool result it carries mentions an error
    assert not keep_line('[supergateway] Child → StreamableHttp: {"result":{"content":[{"text":"error: no such file"}]}}\n')
    # its reports are whole, like the backend's stderr was before quiet filtered stderr: a fix hint has no trouble word
    assert keep_line("[supergateway] Child stderr: fix: run `computer mcp install`\n")
    assert keep_line("[supergateway] Child non-JSON: a stray line on the protocol stream\n")


def test_quiet_passes_only_worth_while_lines_and_the_exit_status_of_the_command() -> None:
    script = 'print("{\\"severity\\":\\"INFO\\"}"); print("a warning"); raise SystemExit(3)'
    result = subprocess.run(
        _quiet(sys.executable, "-c", script),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == "a warning\n"
    assert result.returncode == 3


def test_quiet_drops_the_proxys_info_access_lines_on_stderr_and_keeps_errors_on_their_own_stream() -> None:
    # mcp-auth-proxy's zap logger writes its per-request JSON access records (OAuth query strings included) to stderr.
    script = (
        "import sys; "
        'sys.stderr.write(\'{"severity":"INFO","message":"GET /.idp/auth?client_id=x&state=y"}\\n\'); '
        'sys.stderr.write(\'{"severity":"ERROR","message":"boom"}\\n\'); '
        "sys.stderr.write('a warning on stderr\\n'); "
        "sys.stderr.write('[supergateway] Child exited: code=5, signal=null\\n'); "
        "print('{\"severity\":\"INFO\"}'); print('kept on stdout')"
    )
    result = subprocess.run(_quiet(sys.executable, "-c", script), capture_output=True, text=True, check=False)
    assert result.stderr == (
        '{"severity":"ERROR","message":"boom"}\na warning on stderr\n[supergateway] Child exited: code=5, signal=null\n'
    )
    assert result.stdout == "kept on stdout\n"
    assert result.returncode == 0


def _line_within(stream: IO[bytes], seconds: float) -> bytes:
    """One line of an unbuffered pipe, or what arrived of it when `seconds` run out (a blocked pump never delivers it)."""
    deadline = time.monotonic() + seconds
    data = b""
    while not data.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            break
        chunk = os.read(stream.fileno(), 1)
        if not chunk:
            break
        data += chunk
    return data


def test_an_idle_stream_does_not_hold_back_the_other_one() -> None:
    # The child writes to each stream once and then sleeps with both open: a quiet that drained one stream to its end before
    # reading the other would hold the second line back for as long as the child lives.
    script = (
        "import sys, time; "
        'sys.stderr.write(\'{"severity":"INFO"}\\nerror on stderr\\n\'); sys.stderr.flush(); '
        'sys.stdout.write(\'{"severity":"INFO"}\\nwarn on stdout\\n\'); sys.stdout.flush(); '
        "time.sleep(60)"
    )
    with _quiet_process(script) as process:
        assert process.stdout is not None and process.stderr is not None
        assert _line_within(process.stderr, 10) == b"error on stderr\n"
        assert _line_within(process.stdout, 10) == b"warn on stdout\n"


def test_quiet_fails_and_stops_the_command_when_its_output_cannot_be_written(tmp_path: Path) -> None:
    # A pump that dies of the write error stops reading; unnoticed, the command blocks on the full pipe and quiet waits forever.
    # The command shrugs off the broken pipe, so only quiet stopping it ends it (under `run` nothing else would).
    pid_file = tmp_path / "command.pid"
    script = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGPIPE, signal.SIG_IGN)\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    try:\n"
        "        os.write(1, b'a warning that never ends\\n')\n"
        "    except OSError:\n"
        "        time.sleep(0.01)\n"
    )
    with _quiet_process(script) as process:
        assert process.stdout is not None
        process.stdout.close()  # the reader is gone: every write of quiet fails
        assert process.wait(30) > 0  # a status of its own, not a death by signal (an abort would dump quiet's memory)
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


def test_quiet_ends_with_its_command_even_when_a_descendant_keeps_a_stream_open() -> None:
    # The descendant inherits stderr: waiting for that stream's end would keep quiet (and the unit) up after the command died.
    script = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=subprocess.DEVNULL); "
        "raise SystemExit(7)"
    )
    with _quiet_process(script) as process:
        assert process.wait(10) == 7


def test_quiet_ends_with_its_command_while_a_descendant_floods_an_output_nobody_reads() -> None:
    # The stderr pump blocks writing to a full pipe; a pump that held Python's stderr lock there could keep quiet from exiting.
    flood = "import sys\nwhile True: sys.stderr.write('error flood\\n')"
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {flood!r}], stdout=subprocess.DEVNULL); "
        "time.sleep(0.5); raise SystemExit(7)"
    )
    with _quiet_process(script) as process:
        assert process.wait(15) == 7  # its stderr is never read


def test_quiet_passes_a_line_its_own_encoding_cannot_represent_unchanged() -> None:
    # Re-encoding the command's bytes through quiet's text streams failed on such a line and lost it.
    script = "import sys; sys.stdout.buffer.write('a warning \\u2713\\n'.encode())"
    env = {**os.environ, "PYTHONIOENCODING": "ascii"}
    result = subprocess.run(_quiet(sys.executable, "-c", script), capture_output=True, env=env, timeout=30, check=False)
    assert result.stdout == "a warning \u2713\n".encode()
    assert result.returncode == 0


def test_a_stopped_quiet_dies_of_the_signal_so_systemd_counts_the_stop_as_clean() -> None:
    # A child killed by SIGTERM has returncode -15; returned as quiet's own exit status it became 241, which systemd records as a
    # failure of the bridge unit on every stop.
    script = "import time; print('ready', flush=True); time.sleep(60)"
    with _quiet_process(script) as process:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"
        process.send_signal(signal.SIGTERM)
        assert process.wait(10) == -signal.SIGTERM


def test_a_killed_command_kills_quiet_with_the_same_signal() -> None:
    script = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    result = subprocess.run(_quiet(sys.executable, "-c", script), capture_output=True, check=False)
    assert result.returncode == -signal.SIGKILL


def test_a_crashed_command_is_reported_as_the_shell_status_so_quiet_dumps_no_core() -> None:
    script = "import os, resource, signal; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); os.kill(os.getpid(), signal.SIGABRT)"
    result = subprocess.run(_quiet(sys.executable, "-c", script), capture_output=True, check=False)
    assert result.returncode == 128 + signal.SIGABRT


def test_a_signal_quiet_cannot_raise_becomes_the_shell_status() -> None:
    # Signal 32 is reserved by glibc (>= 2.34 installs no handler for it, so a process dies of it): Python cannot reset its
    # disposition, so quiet reports 128+32 instead of crashing with exit 1.
    script = "import os; os.kill(os.getpid(), 32)"
    if subprocess.run([sys.executable, "-c", script], check=False).returncode != -32:
        pytest.skip("this libc does not let a process die of signal 32")
    result = subprocess.run(_quiet(sys.executable, "-c", script), capture_output=True, check=False)
    assert result.returncode == 128 + 32


def test_a_dash_dash_inside_the_command_is_left_alone() -> None:
    script = "import sys; print(' '.join(sys.argv[1:]))"
    result = subprocess.run(
        _quiet(sys.executable, "-c", script, "a", "--", "b"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == "a -- b\n"


@contextlib.contextmanager
def _restored_stop_handlers() -> Generator[None]:
    """run_foreground installs SIGTERM/SIGINT handlers for the real `run` process; in pytest they would outlive the test."""
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


def test_the_supervise_tests_leave_the_stop_handlers_as_they_found_them() -> None:
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    short = Process("short", "d", (sys.executable, "-c", "raise SystemExit(0)"))
    with _restored_stop_handlers():
        assert run_foreground([short], settle_seconds=0.05) == 0
        assert signal.getsignal(signal.SIGTERM) not in (before[0], None)  # supervise really replaced it
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


def test_the_foreground_run_stops_everything_when_one_process_exits(tmp_path: object) -> None:
    long = Process("long", "d", (sys.executable, "-c", "import time; time.sleep(60)"))
    short = Process("short", "d", (sys.executable, "-c", "raise SystemExit(7)"))
    started = time.monotonic()
    with _restored_stop_handlers():
        assert run_foreground([long, short], settle_seconds=0.05) == 7
    assert time.monotonic() - started < 10


def test_the_password_reaches_a_process_environment_not_its_argv(tmp_path: object) -> None:
    secret_file = Path(str(tmp_path)) / "auth.env"
    secret_file.write_text("PASSWORD=s3cret\n")
    out = Path(str(tmp_path)) / "seen"
    code = f"import os, sys; open({str(out)!r}, 'w').write(os.environ.get('PASSWORD', '') + '|' + ' '.join(sys.argv))"
    process = Process("p", "d", (sys.executable, "-c", code), env_files=(secret_file,))
    with _restored_stop_handlers():
        assert run_foreground([process], settle_seconds=0.05) == 0
    seen = out.read_text()
    assert seen.startswith("s3cret|")
    assert "s3cret" not in seen.split("|", 1)[1]


def test_a_foreground_run_whose_process_is_stopped_by_sigterm_dies_of_sigterm_after_stopping_the_rest(tmp_path: Path) -> None:
    # Returned as a status, the child's -15 became exit 241, which hides a clean stop behind a strange code.
    pid_file = tmp_path / "sibling.pid"
    sibling = (
        f"import os, time; open({str(pid_file)!r} + '.new', 'w').write(str(os.getpid())); "
        f"os.replace({str(pid_file)!r} + '.new', {str(pid_file)!r}); time.sleep(60)"
    )
    # Signals itself only once the sibling is up, so a slow start cannot leave the pid file unwritten; the deadline keeps it from
    # outliving the test should the sibling never come up.
    stopped = (
        "import os, signal, time\n"
        "deadline = time.monotonic() + 20\n"
        f"while not os.path.exists({str(pid_file)!r}) and time.monotonic() < deadline: time.sleep(0.01)\n"
        f"{SELF_SIGTERM}"
    )
    driver = (
        "import sys\n"
        "from computerlib.targets.chatgpt.stack import Process\n"
        "from computerlib.targets.chatgpt.supervise import run_foreground\n"
        f"sibling = Process('sibling', 'd', (sys.executable, '-c', {sibling!r}))\n"
        f"stopped = Process('stopped', 'd', (sys.executable, '-c', {stopped!r}))\n"
        "sys.exit(run_foreground([sibling, stopped], settle_seconds=0.05))\n"
    )
    result = subprocess.run([sys.executable, "-c", driver], capture_output=True, timeout=30, check=False)
    assert result.returncode == -signal.SIGTERM, result.stderr
    sibling_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):  # stopped before run died, not orphaned
        os.kill(sibling_pid, 0)


def test_a_foreground_run_whose_process_crashes_reports_the_shell_status_and_dumps_no_core() -> None:
    core = "import os, resource, signal; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); os.kill(os.getpid(), signal.SIGABRT)"
    driver = (
        "import resource, sys\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        "from computerlib.targets.chatgpt.stack import Process\n"
        "from computerlib.targets.chatgpt.supervise import run_foreground\n"
        f"sys.exit(run_foreground([Process('crashed', 'd', (sys.executable, '-c', {core!r}))], settle_seconds=0.05))\n"
    )
    result = subprocess.run([sys.executable, "-c", driver], capture_output=True, timeout=30, check=False)
    assert result.returncode == 128 + signal.SIGABRT, result.stderr


def test_quiet_and_the_foreground_run_end_like_their_child_through_one_helper() -> None:
    # In a process of its own: should a caller ever bypass the shared helper, the real one kills that process, not pytest.
    driver = (
        "import argparse, sys\n"
        "from computerlib import childexit\n"
        "from computerlib.targets.chatgpt import supervise\n"
        "from computerlib.targets.chatgpt.commands import quiet\n"
        "from computerlib.targets.chatgpt.stack import Process\n"
        "codes = []\n"
        "childexit.exit_like = lambda code: codes.append(code) or 99\n"
        f"stopped = Process('stopped', 'd', (sys.executable, '-c', {SELF_SIGTERM!r}))\n"
        "assert supervise.run_foreground([stopped], settle_seconds=0.05) == 99\n"
        f"assert quiet.run(argparse.Namespace(argv=['--', sys.executable, '-c', {SELF_SIGTERM!r}])) == 99\n"
        "print(codes)\n"
    )
    result = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == f"[{-signal.SIGTERM}, {-signal.SIGTERM}]"
