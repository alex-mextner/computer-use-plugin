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

import sys
from pathlib import Path

import pytest

from computerlib.mcp_probe import HandshakeError, handshake
from computerlib.pins import DESKTOP_COMMANDER_VERSION
from tests.real_dc import ENTRY, REAL_DC, real_dc_home

pytestmark = REAL_DC
# Desktop Commander's flag document as served (2026-11-18): the welcome page on, 100% in the variant that opens it.
WELCOME_PAGE_FOR_EVERYONE: dict[str, object] = {
    "welcome_page_enabled": True,
    "experiments": {"OnboardingPreTool": {"variants": [{"name": "showOnboardingPage", "weight": 100}]}},
}


def test_first_run_answers_initialize_without_opening_a_browser(tmp_path: Path) -> None:
    fresh = real_dc_home(tmp_path, WELCOME_PAGE_FOR_EVERYONE)

    result = handshake(sys.executable, ["-m", "computerlib.cli", "mcp", "serve"], fresh.env, timeout=10)

    assert result.server_name == "desktop-commander"
    assert result.server_version == DESKTOP_COMMANDER_VERSION
    assert "start_process" in result.tools
    assert not fresh.opened.exists(), f"a browser was started: {fresh.opened.read_text()}"
    assert (fresh.dc_config / "config.json").is_file(), "the scratch HOME was not Desktop Commander's first run"


def test_control_without_no_onboarding_waits_on_the_welcome_page(tmp_path: Path) -> None:
    """The same fresh HOME without `--no-onboarding` must hang on xdg-open; otherwise the test above no longer guards #21."""
    fresh = real_dc_home(tmp_path, WELCOME_PAGE_FOR_EVERYONE)

    with pytest.raises(HandshakeError, match="timed out"):
        handshake(fresh.node, [str(ENTRY)], fresh.env, timeout=10)

    assert fresh.opened.is_file(), "Desktop Commander no longer opens its welcome page: the --no-onboarding test guards nothing"
    assert "desktopcommander.app/welcome" in fresh.opened.read_text()
