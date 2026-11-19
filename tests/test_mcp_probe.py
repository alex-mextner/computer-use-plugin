from __future__ import annotations

import select
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from computerlib import mcp_probe
from computerlib.mcp_probe import HandshakeError, handshake
from tests.fakes import fake_computer, minimal_env, write_executable


def test_handshake_lists_tools(tmp_path: Path) -> None:
    server = fake_computer(tmp_path)
    result = handshake(str(server), ["a", "b"], minimal_env(tmp_path), timeout=10)
    assert result.server_name == "stub-dc"
    assert result.server_version == "a b"
    assert result.tools == ("start_process", "read_file", "write_file")


def test_handshake_reports_no_tools(tmp_path: Path) -> None:
    server = fake_computer(tmp_path, "--no-tools")
    assert handshake(str(server), [], minimal_env(tmp_path), timeout=10).tools == ()


def test_handshake_times_out_on_a_silent_server(tmp_path: Path) -> None:
    server = fake_computer(tmp_path, "--hang")
    with pytest.raises(HandshakeError, match="timed out"):
        handshake(str(server), [], minimal_env(tmp_path), timeout=1)


EARLY_EXIT = r"^server exited before answering \(exit 3\)$"


def _spawn_then(monkeypatch: pytest.MonkeyPatch, after: Callable[[subprocess.Popen[bytes]], object]) -> None:
    """Have the probe's Popen run `after` on the started server before the probe gets it."""
    spawn = subprocess.Popen

    # Any mirrors Popen's overloaded signature; the probe always opens byte pipes.
    def spawn_then(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = cast("subprocess.Popen[bytes]", spawn(*args, **kwargs))
        after(proc)
        return proc

    monkeypatch.setattr(mcp_probe.subprocess, "Popen", spawn_then)


def _before_early_exit(monkeypatch: pytest.MonkeyPatch, before: Callable[[subprocess.Popen[bytes]], object]) -> None:
    """Run `before` each time the probe, having lost the server's pipes, starts waiting for it to exit."""
    early_exit = mcp_probe._early_exit  # pyright: ignore[reportPrivateUsage]

    def then_wait(proc: subprocess.Popen[bytes], deadline: float) -> HandshakeError:
        before(proc)
        return early_exit(proc, deadline)

    monkeypatch.setattr(mcp_probe, "_early_exit", then_wait)


def test_a_server_that_dies_before_reading_fails_the_write_and_is_reported_as_an_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_then(monkeypatch, lambda proc: proc.wait())  # its stdin has no reader now, so the first request's write fails
    server = write_executable(tmp_path / "dies", "#!/bin/sh\nexit 3\n")
    with pytest.raises(HandshakeError, match=EARLY_EXIT) as raised:
        handshake(str(server), [], minimal_env(tmp_path), timeout=10)
    assert isinstance(_probe_failure_cause(raised.value), BrokenPipeError)  # the write path really ran


def test_a_server_that_reads_then_dies_ends_the_read_and_is_reported_as_the_same_exit(tmp_path: Path) -> None:
    # The request fits the pipe buffer, so the write succeeds whenever the server reads it; the reply read then hits EOF.
    server = write_executable(tmp_path / "reads_then_dies", "#!/bin/sh\nread -r line\nexit 3\n")
    with pytest.raises(HandshakeError, match=EARLY_EXIT) as raised:
        handshake(str(server), [], minimal_env(tmp_path), timeout=10)
    assert _probe_failure_cause(raised.value) is None  # the read path: no failed write behind it


# Closes both pipes, so the probe can neither write to it nor read from it, then lingers far past the probe's timeout.
PIPES_CLOSED = "exec 0<&- 1>&-\nexec /bin/sleep 60\n"
TIMED_OUT = r"^timed out waiting for the server$"
LINGER_TIMEOUT = 1


def _until_its_pipes_are_closed(proc: subprocess.Popen[bytes]) -> None:
    """Return once the server closed both pipes: polled for no event, our end of each still reports the hang-up (POLLERR on
    stdin, POLLHUP on stdout). Bounded, so a server that never closes them fails the test instead of hanging it."""
    assert proc.stdin is not None and proc.stdout is not None
    poller = select.poll()
    open_fds = {proc.stdin.fileno(), proc.stdout.fileno()}
    for fd in open_fds:
        poller.register(fd, 0)
    deadline = time.monotonic() + 10
    while open_fds:
        remaining = deadline - time.monotonic()
        assert remaining > 0, "the server never closed its pipes"
        for fd, _ in poller.poll(remaining * 1000):
            poller.unregister(fd)
            open_fds.discard(fd)


def test_a_server_that_closes_its_pipes_before_reading_and_lingers_times_out_on_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_then(monkeypatch, _until_its_pipes_are_closed)  # so the first request's write fails with a broken pipe
    server = write_executable(tmp_path / "closes_then_lingers", f"#!/bin/sh\n{PIPES_CLOSED}")
    started = time.monotonic()
    with pytest.raises(HandshakeError, match=TIMED_OUT) as raised:
        handshake(str(server), [], minimal_env(tmp_path), timeout=LINGER_TIMEOUT)
    assert time.monotonic() - started < LINGER_TIMEOUT + 1  # bounded by the timeout, not by the server's exit
    assert isinstance(_probe_failure_cause(raised.value), BrokenPipeError)  # the write path really ran


def test_a_server_that_reads_then_closes_its_pipes_and_lingers_times_out_after_the_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lost: list[subprocess.Popen[bytes]] = []
    _before_early_exit(monkeypatch, lost.append)
    # The request fits the pipe buffer and the server reads it before closing stdin, so the write succeeds; the read hits EOF.
    server = write_executable(tmp_path / "reads_closes_lingers", f"#!/bin/sh\nread -r line\n{PIPES_CLOSED}")
    started = time.monotonic()
    with pytest.raises(HandshakeError, match=TIMED_OUT) as raised:
        handshake(str(server), [], minimal_env(tmp_path), timeout=LINGER_TIMEOUT)
    assert time.monotonic() - started < LINGER_TIMEOUT + 1  # bounded by the timeout, not by the server's exit
    assert len(lost) == 1  # the EOF, not a reply wait that ran out
    assert _probe_failure_cause(raised.value) is None  # the read path: no failed write behind it


def test_a_server_that_exits_only_after_the_probe_saw_eof_is_reported_with_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After closing stdout the server waits for one more line on stdin, so it is still running when the probe starts waiting
    # for its exit; that line is sent only then.
    def let_it_exit(proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdin is not None and proc.poll() is None
        proc.stdin.write(b"\n")
        proc.stdin.flush()

    _before_early_exit(monkeypatch, let_it_exit)
    server = write_executable(tmp_path / "exits_late", "#!/bin/sh\nread -r line\nexec 1>&-\nread -r _\nexit 3\n")
    with pytest.raises(HandshakeError, match=EARLY_EXIT) as raised:
        handshake(str(server), [], minimal_env(tmp_path), timeout=10)
    assert _probe_failure_cause(raised.value) is None  # the read path's EOF came first


def _probe_failure_cause(error: HandshakeError) -> BaseException | None:
    """What made the probe give up: handshake() re-raises the probe's error with the server's stderr appended."""
    assert error.__cause__ is not None
    return error.__cause__.__cause__


def test_handshake_reports_unstartable_command(tmp_path: Path) -> None:
    with pytest.raises(HandshakeError, match="cannot start"):
        handshake(str(tmp_path / "missing"), [], minimal_env(tmp_path), timeout=10)


def test_handshake_ignores_noise_and_server_requests_that_reuse_our_ids(tmp_path: Path) -> None:
    script = tmp_path / "noisy.py"
    script.write_text(
        "import json, sys\n"
        "print('log line that is not json', flush=True)\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if 'id' not in m:\n"
        "        continue\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'method': 'roots/list'}), flush=True)\n"
        "    tools = {'tools': [{'name': 'read_file'}]}\n"
        "    info = {'serverInfo': {'name': 'n', 'version': '1'}}\n"
        "    result = tools if m['method'] == 'tools/list' else info\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': result}), flush=True)\n"
    )
    server = write_executable(tmp_path / "noisy", f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    assert handshake(str(server), [], minimal_env(tmp_path), timeout=10).tools == ("read_file",)


def test_handshake_does_not_take_a_boolean_id_for_a_reply(tmp_path: Path) -> None:
    script = tmp_path / "bool_id.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if 'id' in m:\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': True, 'result': {}}), flush=True)\n"
    )
    server = write_executable(tmp_path / "bool_id", f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    with pytest.raises(HandshakeError, match="timed out"):
        handshake(str(server), [], minimal_env(tmp_path), timeout=1)


def test_handshake_survives_a_server_that_closes_stdin_without_reading(tmp_path: Path) -> None:
    server = write_executable(tmp_path / "deaf", "#!/bin/sh\nexec 0<&-\nexec /bin/sleep 3\n")
    with pytest.raises(HandshakeError):
        handshake(str(server), [], minimal_env(tmp_path), timeout=10)


def test_handshake_error_quotes_the_servers_stderr(tmp_path: Path) -> None:
    server = write_executable(tmp_path / "noisy_death", "#!/bin/sh\necho 'run computer mcp install' >&2\nexit 5\n")
    with pytest.raises(HandshakeError, match="run computer mcp install"):
        handshake(str(server), [], minimal_env(tmp_path), timeout=10)
