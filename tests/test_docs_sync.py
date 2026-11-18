"""The README documents exactly what the CLI has: every command, every flag, the parser description, and the comparison table
`computer chatgpt init` prints."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from computerlib.cli import build_parser
from computerlib.targets.chatgpt import compare

README = Path(__file__).resolve().parent.parent / "README.md"


def subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """The sub-parsers one level below `parser`, discovered from the parser itself."""
    found: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage,reportUnknownVariableType]
            found.update(action.choices)  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
    return found


def command_parsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """`computer <target> <command>` -> its parser, for every command the parser registers."""
    return {
        f"computer {target} {name}": command
        for target, sub in subcommands(parser).items()
        for name, command in subcommands(sub).items()
    }


def test_readme_documents_every_command() -> None:
    text = README.read_text()
    commands = list(command_parsers(build_parser()))
    assert commands, "parser registered no commands"
    missing = [c for c in commands if c not in text]
    assert not missing, f"README.md does not mention: {missing}"


def test_readme_documents_every_flag_of_every_command() -> None:
    text = README.read_text()
    missing = [
        f"{line} {flag}"
        for line, parser in command_parsers(build_parser()).items()
        for flag in sorted(set(re.findall(r"--[a-z][a-z-]*", parser.format_help())) - {"--help"})
        if f"`{flag}`" not in text
    ]
    assert missing == []


def test_readme_carries_the_parser_description() -> None:
    description = build_parser().description
    assert description is not None
    assert description in README.read_text()


def test_the_readme_table_is_the_one_init_prints() -> None:
    printed = compare.render("en").split("\n\n")[1]
    assert printed in README.read_text()
