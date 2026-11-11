"""XDG base directories, resolved at call time so tests and users can redirect them with the environment.

An XDG variable that is not an absolute path is ignored, as the XDG spec requires (a relative one would resolve against whatever
directory a desktop app happens to start in).
"""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(variable: str) -> Path | None:
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else None


def explicit_data_home() -> Path | None:
    """XDG_DATA_HOME when the environment sets a valid one, else None (the default location needs no override)."""
    return _xdg("XDG_DATA_HOME")


def explicit_config_home() -> Path | None:
    return _xdg("XDG_CONFIG_HOME")


def config_home() -> Path:
    return explicit_config_home() or Path.home() / ".config"


def data_home() -> Path:
    return explicit_data_home() or Path.home() / ".local/share"
