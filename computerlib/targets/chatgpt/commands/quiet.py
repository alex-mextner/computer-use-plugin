"""`computer chatgpt quiet -- COMMAND [ARGS...]` (internal; the bridge unit runs it): run COMMAND with its stdout cut down to what
is worth a journal line. stderr is untouched and the exit status is the command's own.

Why: mcp-auth-proxy writes one JSON access line per request to stdout and the supergateway it starts for the stdio backend writes
a copy of EVERY JSON-RPC message (tool arguments and results included) plus chatter per request. The journal is no place for that
(disk, privacy); the shim's access log keeps the compact record.
  dropped: the proxy's INFO access lines; supergateway's message copies; other supergateway lines unless they mention
           error, warn, fail, exception, fatal, panic, refused or timeout
  kept:    everything else (startup problems, warnings, errors, panics)
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys

from agenttools_errors import UsageError

from computerlib.commandtree import Command

TROUBLE_WORDS = ("error", "warn", "fail", "exception", "fatal", "panic", "refused", "timeout")


def keep_line(line: str) -> bool:
    """Whether a stdout line of the bridge belongs in the journal."""
    if '"severity":"INFO"' in line:
        return False
    if line.startswith("[supergateway] "):
        if line.startswith(("[supergateway] StreamableHttp ", "[supergateway] Child ")):
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
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, errors="replace", bufsize=1)

    def forward(signum: int, _frame: object) -> None:
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    assert child.stdout is not None
    for line in child.stdout:
        if keep_line(line):
            sys.stdout.write(line)
            sys.stdout.flush()
    return child.wait()


COMMAND = Command(
    name="quiet", help="(internal) run a command with its noisy stdout filtered, for the journal", run=run, configure=configure
)
