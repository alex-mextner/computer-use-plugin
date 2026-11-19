"""`computer chatgpt quiet -- COMMAND [ARGS...]` (internal; the bridge unit runs it): run COMMAND with its stdout and stderr cut
down to what is worth a journal line, each kept line on the stream it came from. Each stream has its own pump, so a stream that
stays silent never holds back the other. The exit status is the command's own, by `childexit.exit_like` (shared with `run`):
when the command dies of a signal, quiet dies of the same signal, so systemd sees a stop by SIGTERM as clean.
quiet ends with the command, not with its streams: a descendant that inherited one would otherwise keep quiet (and the unit) up,
so what is still buffered gets DRAIN_SECONDS. A line quiet cannot write (the journal gone) stops the command and fails quiet: the
pump would stop reading, and the command would block on the full pipe forever. Lines go out as the command's own bytes straight to
fd 1 and 2: re-encoding them through quiet's text streams fails on a line its locale cannot represent, and a pump blocked inside
Python's buffered stream holds the lock interpreter shutdown needs.

Why: mcp-auth-proxy (zap logger) writes one JSON access line per request to stderr, OAuth query strings (client_id,
redirect_uri, state, ...) included, and the supergateway it starts for the stdio backend writes a copy of EVERY JSON-RPC message
(tool arguments and results included) plus chatter per request. The journal is no place for that (disk, privacy); the shim's
access log keeps the compact record.
  dropped: the proxy's INFO lines (its access records among them, on stderr); supergateway's message copies; other supergateway
           lines unless they mention error, warn, fail, exception, fatal, panic, refused or timeout
  kept:    everything else (startup problems, warnings, errors, panics), and whole: supergateway's error-level reports about the
           backend (`Child stderr:`, `Child exited:`, `Child non-JSON:`, on stderr), the only place a backend that fails to start
           says why
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import threading
import time
from typing import IO

from agenttools_errors import UsageError

from computerlib import childexit
from computerlib.commandtree import Command
from computerlib.targets.chatgpt import supervise

TROUBLE_WORDS = ("error", "warn", "fail", "exception", "fatal", "panic", "refused", "timeout")
MESSAGE_COPIES = ("[supergateway] StreamableHttp ", "[supergateway] Child ")
# supergateway's own error-level reports about the backend share the `Child` prefix of its message copies.
BACKEND_REPORTS = ("[supergateway] Child stderr:", "[supergateway] Child exited:", "[supergateway] Child non-JSON:")
DRAIN_SECONDS = 2.0
STDOUT_FD = 1
STDERR_FD = 2


def keep_line(line: str) -> bool:
    """Whether a stdout or stderr line of the bridge belongs in the journal."""
    if '"severity":"INFO"' in line:
        return False
    if line.startswith(BACKEND_REPORTS):
        return True
    if line.startswith("[supergateway] "):
        if line.startswith(MESSAGE_COPIES):
            return False
        return any(word in line.lower() for word in TROUBLE_WORDS)
    return True


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("argv", nargs=argparse.REMAINDER, help="the command to run, after --")


def run(args: argparse.Namespace) -> int:
    argv: list[str] = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise UsageError(
            what="no command to run", why="quiet needs a command after --", fix="computer chatgpt quiet -- <command> [args]"
        )
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def forward(signum: int, _frame: object) -> None:
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    assert child.stdout is not None and child.stderr is not None
    failures: list[OSError] = []

    def pump(source: IO[bytes], target: int) -> None:
        try:
            for line in source:
                if keep_line(line.decode(errors="replace")):
                    _write_all(target, line)
        except OSError as error:
            failures.append(error)  # before stopping the command, so the main thread sees why it ended
            supervise.stop_all([child])

    pumps = [
        threading.Thread(target=pump, args=(child.stdout, STDOUT_FD), daemon=True),
        threading.Thread(target=pump, args=(child.stderr, STDERR_FD), daemon=True),
    ]
    for thread in pumps:
        thread.start()
    child.wait()
    if not failures:
        deadline = time.monotonic() + DRAIN_SECONDS
        for thread in pumps:
            thread.join(max(0.0, deadline - time.monotonic()))
    if failures:  # raised here: only the main thread can fail quiet
        raise failures[0]
    return childexit.exit_like(child.returncode)


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


COMMAND = Command(
    name="quiet", help="(internal) run a command with its noisy output filtered, for the journal", run=run, configure=configure
)
