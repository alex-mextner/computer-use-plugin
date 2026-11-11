"""Two-level self-registration for `computer <target> <command>`.

HOW IT IS REACHED: a target package's `__init__` builds its `TARGET` with `command_target(name, help, __name__)`; every module in
that package's `commands/` sub-package that exposes a module-level `COMMAND: Command` becomes a sub-command. Adding a command is
dropping a module; no registry is edited.
INVARIANTS: a bare `computer <target>` prints that target's help and exits 0; commands return their exit code.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from agenttools_errors import AgentToolError, PermissionDeniedError

from computerlib.targets import Target

EXIT_CODES_EPILOG = """\
exit codes:
  0    ok
  1    internal error or a failed gate
  2    usage or configuration error
  3    status/doctor found the stack unhealthy
  5    a file or binary the command needs is missing
  7    network failure (download, tunnel, DNS)
  8    permission or credential failure
  127  a required external tool (node, systemctl, ...) is not installed
"""


def _no_options(_parser: argparse.ArgumentParser) -> None:
    return None


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    run: Callable[[argparse.Namespace], int]
    configure: Callable[[argparse.ArgumentParser], None] = _no_options


def discover_commands(target_package: str) -> list[Command]:
    commands_package = importlib.import_module(f"{target_package}.commands")
    found: list[Command] = []
    for info in pkgutil.iter_modules(commands_package.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{commands_package.__name__}.{info.name}")
        command: object = getattr(module, "COMMAND", None)
        if isinstance(command, Command):
            found.append(command)
    return sorted(found, key=lambda c: c.name)


def command_target(name: str, help: str, target_package: str) -> Target:
    commands = discover_commands(target_package)

    def configure(parser: argparse.ArgumentParser) -> None:
        subs = parser.add_subparsers(dest="command", metavar="<command>")
        for command in commands:
            sub = subs.add_parser(
                command.name,
                help=command.help,
                description=command.help,
                epilog=EXIT_CODES_EPILOG,
                formatter_class=argparse.RawDescriptionHelpFormatter,
                allow_abbrev=False,
            )
            command.configure(sub)
            sub.set_defaults(_command=command.run)

    def run(args: argparse.Namespace) -> int:
        command_run = cast("Callable[[argparse.Namespace], int] | None", getattr(args, "_command", None))
        if command_run is not None:
            try:
                return command_run(args)
            except PermissionError as exc:
                raise PermissionDeniedError(
                    what=f"permission denied: {exc.filename or exc}", why=str(exc), fix="fix the permissions and re-run"
                ) from exc
            except OSError as exc:
                raise AgentToolError(
                    what=f"file operation failed: {exc.filename or exc}", why=str(exc), fix="fix the cause above and re-run"
                ) from exc
        parser: object = getattr(args, "_parser", None)
        if isinstance(parser, argparse.ArgumentParser):
            parser.print_help()
        return 0

    return Target(name=name, help=help, configure=configure, run=run)
