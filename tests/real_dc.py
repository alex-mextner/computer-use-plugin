"""A scratch HOME in which `computer mcp serve` runs a real, already installed Desktop Commander tree (the opt-in real-DC tests).

COMPUTER_DC_TREE names a tree `computer mcp install` made (e.g. ~/.local/share/computer/desktop-commander/trees/<lock sha256>);
it is linked, never written. `xdg-open` first on PATH records what it was asked to open and never returns (a browser staying in
the foreground). The flag URL and telemetry are switched off, so the seeded flag cache is what Desktop Commander decides by.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from computerlib.desktop_commander import bundled_lock_files
from computerlib.pins import DESKTOP_COMMANDER_PACKAGE, DESKTOP_COMMANDER_VERSION
from tests.fakes import MINIMAL_PATH, write_executable

DC_TREE = os.environ.get("COMPUTER_DC_TREE", "")
REAL_DC = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("COMPUTER_INTEGRATION") == "1" and DC_TREE),
        reason="set COMPUTER_INTEGRATION=1 and COMPUTER_DC_TREE=<an installed Desktop Commander tree> to run the real server",
    ),
]
ENTRY = Path(DC_TREE) / "node_modules" / DESKTOP_COMMANDER_PACKAGE / "dist" / "index.js"


def _real_node() -> str:
    """What `computer mcp install` records: the binary node reports, not a version manager's shim."""
    found = shutil.which("node")
    assert found is not None, "the real server needs node on PATH"
    return subprocess.run([found, "-p", "process.execPath"], capture_output=True, text=True, check=True).stdout.strip()


@dataclass(frozen=True)
class RealDcHome:
    env: dict[str, str]
    node: str
    dc_config: Path
    opened: Path  # what the never-returning xdg-open was asked to open, one line per call


def real_dc_home(tmp_path: Path, flags: dict[str, object]) -> RealDcHome:
    """A HOME with the tree installed as `computer mcp install` records it and `flags` as Desktop Commander's cached flags."""
    home = tmp_path / "home"
    lock_sha256 = hashlib.sha256(bundled_lock_files()[1]).hexdigest()
    managed = home / ".local/share/computer/desktop-commander"
    (managed / "trees").mkdir(parents=True)
    (managed / "trees" / lock_sha256).symlink_to(Path(DC_TREE).resolve())
    node = _real_node()
    record = {
        "package": DESKTOP_COMMANDER_PACKAGE,
        "version": DESKTOP_COMMANDER_VERSION,
        "lock_sha256": lock_sha256,
        "tree": lock_sha256,
        "node": node,
    }
    (managed / "install.json").write_text(json.dumps(record))
    dc_config = home / ".claude-server-commander"
    dc_config.mkdir()
    (dc_config / "feature-flags.json").write_text(json.dumps({"flags": flags}))
    bin_dir = tmp_path / "bin"
    opened = tmp_path / "xdg-open.log"
    write_executable(bin_dir / "xdg-open", f'#!/bin/sh\necho "$@" >> "{opened}"\nexec /bin/sleep 60\n')
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:{MINIMAL_PATH}",
        "DC_FLAG_URL": "http://127.0.0.1:9/flags.json",  # nothing listens: the seeded cache stays as it is
        "DESKTOP_COMMANDER_DISABLE_TELEMETRY": "1",
    }
    return RealDcHome(env=env, node=node, dc_config=dc_config, opened=opened)
