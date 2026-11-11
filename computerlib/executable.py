"""Where the `computer` command lives, as an absolute path a client config can store.

INVARIANTS: the path is NOT resolved through symlinks (`~/.local/bin/computer` stays stable across tool upgrades); it is
absolute so a client that autostarts with a bare PATH still finds it.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from agenttools_errors import MissingTargetError


def computer_executable() -> Path:
    invoked = sys.argv[0]
    if os.path.basename(invoked) == "computer" and os.sep in invoked and os.access(invoked, os.X_OK):
        return Path(os.path.abspath(invoked))
    on_path = shutil.which("computer")
    if on_path is not None:
        return Path(os.path.abspath(on_path))
    sibling = Path(sys.executable).with_name("computer")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return sibling
    raise MissingTargetError(
        what="cannot find the `computer` executable",
        why="it is not on PATH and this process was not started through it",
        fix="install it (`uv tool install <this repo>`) and run the command as `computer ...`",
    )
