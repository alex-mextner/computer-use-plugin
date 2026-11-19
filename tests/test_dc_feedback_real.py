"""The real pinned Desktop Commander behind `computer mcp serve` offers no feedback tool and no survey request (#24).

Opt-in, like tests/test_dc_first_run.py (see tests/real_dc.py for the scratch HOME). The flag cache has `user_surveys` on and
DC's config.json holds the usage of someone who has used it for 5 days and 20 calls without giving feedback, which is when
dist/utils/usageTracker.js `shouldPromptForFeedback` puts a survey on the first successful result of a session. The control runs
start the same tree without serve and must list the tool, start the browser and get the survey: if a Desktop Commander pin bump
drops any of that, they fail instead of the tests through serve staying green while they test nothing.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from computerlib.dc_relay import FEEDBACK_TOOL
from tests.mcp_session import McpSession, result_texts
from tests.real_dc import ENTRY, REAL_DC, RealDcHome, real_dc_home

pytestmark = REAL_DC
SERVE = [sys.executable, "-m", "computerlib.cli", "mcp", "serve"]
SURVEY_TEXT = "Desktop Commander Team request"


def _surveyed_home(tmp_path: Path) -> RealDcHome:
    home = real_dc_home(tmp_path, {"user_surveys": True})
    now_ms = int(time.time() * 1000)
    usage: dict[str, object] = {
        "totalToolCalls": 20,
        "successfulCalls": 20,
        "failedCalls": 0,
        "toolCounts": {},
        "firstUsed": now_ms - 5 * 86_400_000,
        "lastUsed": now_ms - 3_600_000,
        "totalSessions": 4,
        "lastFeedbackPrompt": 0,
    }
    (home.dc_config / "config.json").write_text(json.dumps({"usageStats": usage, "telemetryEnabled": False}))
    return home


def test_serve_lists_no_feedback_tool_and_answers_it_without_a_browser(tmp_path: Path) -> None:
    home = _surveyed_home(tmp_path)
    with McpSession(SERVE, home.env) as session:
        session.initialize(timeout=15)
        tools = session.tool_names(timeout=10)
        result = session.call(FEEDBACK_TOOL, {}, timeout=5)
    assert "start_process" in tools and FEEDBACK_TOOL not in tools
    assert result["isError"] is True
    assert not home.opened.exists(), f"a browser was started: {home.opened.read_text()}"


def test_no_result_through_serve_asks_for_feedback_with_surveys_on(tmp_path: Path) -> None:
    home = _surveyed_home(tmp_path)
    with McpSession(SERVE, home.env) as session:
        session.initialize(timeout=15)
        texts = result_texts(session.call("get_config", {}, timeout=10))
    assert texts and not any(SURVEY_TEXT in text or "[SYSTEM INSTRUCTION]" in text for text in texts)


def test_control_without_serve_dc_lists_the_tool_and_surveys(tmp_path: Path) -> None:
    home = _surveyed_home(tmp_path)
    with McpSession([home.node, str(ENTRY), "--no-onboarding"], home.env) as session:
        session.initialize(timeout=15)
        assert FEEDBACK_TOOL in session.tool_names(timeout=10)
        texts = result_texts(session.call("get_config", {}, timeout=10))
        assert any(SURVEY_TEXT in text for text in texts), "DC no longer adds its survey: the serve test guards nothing"
        with pytest.raises(AssertionError, match="no answer"):
            session.call(FEEDBACK_TOOL, {}, timeout=5)
    assert "tally.so" in home.opened.read_text(), "DC's feedback tool no longer opens a browser"
