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


def test_remove_drops_the_enablement_links_of_the_removed_units_only(tmp_path: Path, machine: FakeMachine) -> None:
    systemd = Systemd(tmp_path)
    (tmp_path / "a.service").write_text("x")
    for target in ("default.target.wants", "multi-user.target.requires"):
        (tmp_path / target).mkdir()
        (tmp_path / target / "a.service").symlink_to(tmp_path / "a.service")
        (tmp_path / target / "other.service").symlink_to(tmp_path / "other.service")
    systemd.remove(["a.service", "b.service"])
    assert sorted(p.name for p in tmp_path.rglob("*.service") if p.is_symlink()) == ["other.service", "other.service"]
    assert not (tmp_path / "a.service").exists()


def test_remove_keeps_a_link_that_still_reaches_a_unit_file_elsewhere(tmp_path: Path, machine: FakeMachine) -> None:
    """A same-named unit in another search path, enabled from this dir, is the user's: its working link survives."""
    units = tmp_path / "units"
    wants = units / "default.target.wants"
    wants.mkdir(parents=True)
    elsewhere = tmp_path / "share" / "a.service"
    elsewhere.parent.mkdir()
    elsewhere.write_text("x")
    (wants / "a.service").symlink_to(elsewhere)
    Systemd(units).remove(["a.service"])
    assert (wants / "a.service").is_symlink()


def test_remove_never_reads_a_unit_name_as_a_glob_pattern(tmp_path: Path, machine: FakeMachine) -> None:
    wants = tmp_path / "default.target.wants"
    wants.mkdir()
    (wants / "mine-shim.service").symlink_to(tmp_path / "mine-shim.service")
    Systemd(tmp_path).remove(["*-shim.service"])
    assert (wants / "mine-shim.service").is_symlink()
