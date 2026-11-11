"""`computer` command line: argparse, two-level self-registration (`computer <target> <command>`).

HOW IT IS REACHED: the `computer` console script (pyproject `[project.scripts]`) calls `main()`.
INVARIANTS: stdlib argparse only (agenttools_errors for exit codes); `main()` returns an exit code and never raises for a user
error; a bare `computer` or `computer <target>` prints help and exits 0, it never starts anything.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from agenttools_errors import guard

from computerlib import package_version
from computerlib.commandtree import EXIT_CODES_EPILOG
from computerlib.targets import discover_targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="computer",
        description="Give a chat client (Claude Desktop today) a shell and files on this PC through MCP (Desktop Commander).",
        epilog=EXIT_CODES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"computer {package_version()}")
    subs = parser.add_subparsers(dest="target", metavar="<target>")
    for target in discover_targets():
        sub = subs.add_parser(
            target.name,
            help=target.help,
            description=target.help,
            epilog=EXIT_CODES_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            allow_abbrev=False,
        )
        target.configure(sub)
        sub.set_defaults(_run=target.run, _parser=sub)
    parser.set_defaults(_parser=parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    run = getattr(args, "_run", None)
    if run is None:
        parser.print_help()
        return 0
    return guard(lambda: run(args))


if __name__ == "__main__":
    sys.exit(main())
