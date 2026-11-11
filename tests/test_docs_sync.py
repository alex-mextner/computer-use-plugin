from __future__ import annotations

import argparse
from pathlib import Path

from computerlib.cli import build_parser

README = Path(__file__).resolve().parent.parent / "README.md"


def command_lines(parser: argparse.ArgumentParser) -> list[str]:
    """Every `computer <target> <command>` the parser registers, discovered from the parser itself."""
    lines: list[str] = []
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if not isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage,reportUnknownVariableType]
            continue
        for target, target_parser in action.choices.items():  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
            for inner in target_parser._actions:  # pyright: ignore[reportUnknownMemberType,reportPrivateUsage,reportUnknownVariableType]
                if isinstance(inner, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage,reportUnknownVariableType]
                    lines.extend(f"computer {target} {name}" for name in inner.choices)  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
    return lines


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
