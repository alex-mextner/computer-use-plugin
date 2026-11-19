"""Run the stack in the foreground without systemd (`computer chatgpt run`): start every process, stop all when one exits or on a
signal.

No restarts here: this is for development and for proving the stack on scratch ports; systemd (Restart=always) is the supervisor
of a real install.

`run` ends the way the process that stopped the stack ended (`childexit.exit_like`, shared with every wrapper), so a stop by
SIGTERM is seen as one: returned as a status, -15 would be exit 241.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from computerlib import childexit
from computerlib.targets.chatgpt.password import PASSWORD_VARIABLE, read_password
from computerlib.targets.chatgpt.stack import Process

STOP_GRACE_SECONDS = 8.0


def _environment(process: Process, base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env.update(process.env)
    for path in process.env_files:
        value = read_password(Path(path))
        if value:
            env[PASSWORD_VARIABLE] = value
    return env


def run_foreground(processes: Sequence[Process], settle_seconds: float = 0.3) -> int:
    """Start the processes in order; when the first one exits, stop the rest and end like it (`childexit.exit_like`); 0 on a
    signal."""
    children: list[subprocess.Popen[bytes]] = []
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        for process in processes:
            children.append(subprocess.Popen(list(process.argv), env=_environment(process, dict(os.environ))))
            time.sleep(settle_seconds)
        while not stopping and all(child.poll() is None for child in children):
            time.sleep(0.2)
        exited = [c.returncode for c in children if c.poll() is not None]
        code = 0 if stopping or not exited else exited[0]
    finally:
        stop_all(children)
    # Only after the rest are stopped: dying of the child's signal any earlier would orphan them.
    return childexit.exit_like(code)


def stop_all(children: Sequence[subprocess.Popen[bytes]]) -> None:
    """Terminate the children still running (last started first), killing any that outlive the grace period, and reap them."""
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    for child in children:
        try:
            child.wait(max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
