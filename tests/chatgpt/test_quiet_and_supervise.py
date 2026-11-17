from __future__ import annotations

import subprocess
import sys
import time

from computerlib.targets.chatgpt.commands.quiet import keep_line
from computerlib.targets.chatgpt.stack import Process
from computerlib.targets.chatgpt.supervise import run_foreground


def test_the_journal_keeps_problems_and_drops_per_request_chatter() -> None:
    assert not keep_line('{"severity":"INFO","message":"request"}\n')
    assert not keep_line("[supergateway] StreamableHttp message: {...tool arguments...}\n")
    assert not keep_line("[supergateway] Child stdout: {...}\n")
    assert not keep_line("[supergateway] listening on 8000\n")
    assert keep_line("[supergateway] fatal: cannot bind port\n")
    assert keep_line('{"severity":"ERROR","message":"boom"}\n')
    assert keep_line("panic: runtime error\n")


def test_quiet_passes_only_worth_while_lines_and_the_exit_status_of_the_command() -> None:
    script = 'print("{\\"severity\\":\\"INFO\\"}"); print("a warning"); raise SystemExit(3)'
    result = subprocess.run(
        [sys.executable, "-m", "computerlib.cli", "chatgpt", "quiet", "--", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == "a warning\n"
    assert result.returncode == 3


def test_a_dash_dash_inside_the_command_is_left_alone() -> None:
    script = "import sys; print(' '.join(sys.argv[1:]))"
    result = subprocess.run(
        [sys.executable, "-m", "computerlib.cli", "chatgpt", "quiet", "--", sys.executable, "-c", script, "a", "--", "b"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == "a -- b\n"


def test_the_foreground_run_stops_everything_when_one_process_exits(tmp_path: object) -> None:
    long = Process("long", "d", (sys.executable, "-c", "import time; time.sleep(60)"))
    short = Process("short", "d", (sys.executable, "-c", "raise SystemExit(7)"))
    started = time.monotonic()
    assert run_foreground([long, short], settle_seconds=0.05) == 7
    assert time.monotonic() - started < 10


def test_the_password_reaches_a_process_environment_not_its_argv(tmp_path: object) -> None:
    from pathlib import Path

    secret_file = Path(str(tmp_path)) / "auth.env"
    secret_file.write_text("PASSWORD=s3cret\n")
    out = Path(str(tmp_path)) / "seen"
    code = f"import os, sys; open({str(out)!r}, 'w').write(os.environ.get('PASSWORD', '') + '|' + ' '.join(sys.argv))"
    process = Process("p", "d", (sys.executable, "-c", code), env_files=(secret_file,))
    assert run_foreground([process], settle_seconds=0.05) == 0
    seen = out.read_text()
    assert seen.startswith("s3cret|")
    assert "s3cret" not in seen.split("|", 1)[1]
