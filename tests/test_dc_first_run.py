"""The real pinned Desktop Commander on its first run in a fresh HOME answers `initialize` through `computer mcp serve` (#21).

Opt-in (it runs the real Node.js package): COMPUTER_INTEGRATION=1 and COMPUTER_DC_TREE=<a tree `computer mcp install` made, e.g.
~/.local/share/computer/desktop-commander/trees/<lock sha256>>; `node` must be on PATH. The tree is linked into a scratch HOME
that Desktop Commander has never seen (no config.json yet), and an `xdg-open` that never returns (a browser staying in the
foreground) is first on its PATH. The flag cache is seeded with the welcome page on for every user, and the flag URL and
telemetry are switched off, so the outcome does not depend on the network or on which A/B arm the random client id lands in:
without `--no-onboarding` the first `initialize` waits on that xdg-open forever. The control run starts the same tree without the
flag and must reach that xdg-open: if a Desktop Commander pin bump drops the welcome-page path, the control fails instead of the
main test staying green while it tests nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from computerlib.desktop_commander import bundled_lock_files
from computerlib.mcp_probe import HandshakeError, handshake
from computerlib.pins import DESKTOP_COMMANDER_PACKAGE, DESKTOP_COMMANDER_VERSION
from tests.fakes import MINIMAL_PATH, write_executable

DC_TREE = os.environ.get("COMPUTER_DC_TREE", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("COMPUTER_INTEGRATION") == "1" and DC_TREE),
        reason="set COMPUTER_INTEGRATION=1 and COMPUTER_DC_TREE=<an installed Desktop Commander tree> to run the real server",
    ),
]
# Desktop Commander's flag document as served (2026-11-18): the welcome page on, 100% in the variant that opens it.
WELCOME_PAGE_FOR_EVERYONE = {
    "flags": {
        "welcome_page_enabled": True,
        "experiments": {"OnboardingPreTool": {"variants": [{"name": "showOnboardingPage", "weight": 100}]}},
    }
}


def _real_node() -> str:
    """What `computer mcp install` records: the binary node reports, not a version manager's shim."""
    found = shutil.which("node")
    assert found is not None, "the real server needs node on PATH"
    return subprocess.run([found, "-p", "process.execPath"], capture_output=True, text=True, check=True).stdout.strip()


@dataclass(frozen=True)
class FreshHome:
    env: dict[str, str]
    node: str
    dc_config: Path
    opened: Path  # what the never-returning xdg-open was asked to open, one line per call


def _fresh_home(tmp_path: Path) -> FreshHome:
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
    (dc_config / "feature-flags.json").write_text(json.dumps(WELCOME_PAGE_FOR_EVERYONE))
    bin_dir = tmp_path / "bin"
    opened = tmp_path / "xdg-open.log"
    write_executable(bin_dir / "xdg-open", f'#!/bin/sh\necho "$@" >> "{opened}"\nexec /bin/sleep 60\n')
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:{MINIMAL_PATH}",
        "DC_FLAG_URL": "http://127.0.0.1:9/flags.json",  # nothing listens: the seeded cache stays as it is
        "DESKTOP_COMMANDER_DISABLE_TELEMETRY": "1",
    }
    return FreshHome(env=env, node=node, dc_config=dc_config, opened=opened)


def test_first_run_answers_initialize_without_opening_a_browser(tmp_path: Path) -> None:
    fresh = _fresh_home(tmp_path)

    result = handshake(sys.executable, ["-m", "computerlib.cli", "mcp", "serve"], fresh.env, timeout=10)

    assert result.server_name == "desktop-commander"
    assert result.server_version == DESKTOP_COMMANDER_VERSION
    assert "start_process" in result.tools
    assert not fresh.opened.exists(), f"a browser was started: {fresh.opened.read_text()}"
    assert (fresh.dc_config / "config.json").is_file(), "the scratch HOME was not Desktop Commander's first run"


def test_control_without_no_onboarding_waits_on_the_welcome_page(tmp_path: Path) -> None:
    """The same fresh HOME without `--no-onboarding` must hang on xdg-open; otherwise the test above no longer guards #21."""
    fresh = _fresh_home(tmp_path)
    entry = Path(DC_TREE) / "node_modules" / DESKTOP_COMMANDER_PACKAGE / "dist" / "index.js"

    with pytest.raises(HandshakeError, match="timed out"):
        handshake(fresh.node, [str(entry)], fresh.env, timeout=10)

    assert fresh.opened.is_file(), "Desktop Commander no longer opens its welcome page: the --no-onboarding test guards nothing"
    assert "desktopcommander.app/welcome" in fresh.opened.read_text()
