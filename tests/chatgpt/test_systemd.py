from __future__ import annotations

from pathlib import Path

import pytest
from agenttools_errors import AgentToolError

from computerlib.targets.chatgpt.systemd import Systemd
from tests.chatgpt.conftest import FakeMachine


def test_writing_the_same_units_again_reloads_nothing(tmp_path: Path, machine: FakeMachine) -> None:
    systemd = Systemd(tmp_path / "units")
    assert systemd.install({"a.service": "[Unit]\n"}) == ["a.service"]
    assert systemd.install({"a.service": "[Unit]\n"}) == []
    assert machine.calls().count("daemon-reload") == 1


def test_a_changed_unit_is_rewritten_and_reloaded(tmp_path: Path, machine: FakeMachine) -> None:
    systemd = Systemd(tmp_path / "units")
    systemd.install({"a.service": "one"})
    assert systemd.install({"a.service": "two"}) == ["a.service"]
    assert (tmp_path / "units" / "a.service").read_text() == "two"
    assert machine.calls().count("daemon-reload") == 2


def test_state_reports_active_and_enabled_after_enable_now(tmp_path: Path, machine: FakeMachine) -> None:
    systemd = Systemd(tmp_path)
    systemd.enable_now(["a.service"])
    state = systemd.state("a.service")
    assert (state.active, state.enabled) == ("active", True)
    systemd.disable_now(["a.service"])
    state = systemd.state("a.service")
    assert (state.active, state.enabled) == ("inactive", False)


def test_without_systemctl_the_error_says_how_to_run_without_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(AgentToolError) as caught:
        Systemd(tmp_path).enable_now(["a.service"])
    assert caught.value.exit_code == 127
    assert "computer chatgpt run" in caught.value.fix


def test_lingering_can_be_read_and_enabled(tmp_path: Path, machine: FakeMachine) -> None:
    systemd = Systemd(tmp_path)
    assert not systemd.lingering("someone")
    systemd.enable_lingering("someone")
    assert systemd.lingering("someone")
