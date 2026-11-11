from __future__ import annotations

import argparse
from pathlib import Path

from computerlib.cli import build_parser

README = Path(__file__).resolve().parent.parent / "README.md"


def subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """The sub-parsers one level below `parser`, discovered from the parser itself."""
    found: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage,reportUnknownVariableType]
            found.update(action.choices)  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
    return found


def command_lines(parser: argparse.ArgumentParser) -> list[str]:
    """Every `computer <target> <command>` the parser registers."""
    return [f"computer {target} {name}" for target, sub in subcommands(parser).items() for name in subcommands(sub)]


def test_readme_documents_every_command() -> None:
    text = README.read_text()
    commands = command_lines(build_parser())
    assert commands, "parser registered no commands"
    missing = [c for c in commands if c not in text]
    assert not missing, f"README.md does not mention: {missing}"


def test_readme_carries_the_parser_description() -> None:
    description = build_parser().description
    assert description is not None
    assert description in README.read_text()
