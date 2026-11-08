"""Target discovery: `computer <target> <command>`.

HOW IT IS REACHED: `computerlib.cli.build_parser` calls `discover_targets()`; every sub-package of this package whose name does
not start with an underscore and that exposes a module-level `TARGET: Target` is a target. Adding a target is dropping a package
here; no registry is edited (skill self-registering-commands).
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """`configure` adds the target's own sub-commands to its parser; `run` dispatches the parsed namespace."""

    name: str
    help: str
    configure: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], int]


def discover_targets() -> list[Target]:
    found: list[Target] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        target: object = getattr(module, "TARGET", None)
        if isinstance(target, Target):
            found.append(target)
    return sorted(found, key=lambda t: t.name)
