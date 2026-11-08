"""computer: connect chat clients to this PC's computer-use MCP server.

The version is read from the installed distribution's metadata, never from a literal (a literal goes stale).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

DIST_NAME = "hyper-computer"


def package_version() -> str:
    try:
        return version(DIST_NAME)
    except PackageNotFoundError:
        return "0+unknown"
