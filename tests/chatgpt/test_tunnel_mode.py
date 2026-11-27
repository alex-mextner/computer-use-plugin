"""The tunnel mode as a user runs it: init, login, start/stop/restart/end/uninstall, status, doctor, connect, run, against a fake
systemd and a stand-in for tunnel-client's health port. The real daemon is exercised, opt-in, in test_tunnel_end_to_end.py.

Contract under test: `public` is the default and nothing ever switches the mode; one unit; the runtime key lives in a 0600 file
and is never printed, never in an argument list or a unit; `connected` needs a tool call through the tunnel.
"""

from __future__ import annotations

import io
import json
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from computerlib import desktop_commander
from computerlib.targets.chatgpt import dialog, lifecycle, processes
from computerlib.targets.chatgpt.commands.connect import watch_tunnel
from computerlib.targets.chatgpt.context import load_context
from computerlib.targets.chatgpt.settings import Settings, config_file, load_settings, save_settings
from computerlib.targets.chatgpt.tunnel import write_key
from tests.chatgpt.cli_helpers import (
    RUNTIME_KEY,
    TUNNEL_ID,
    init_args,
    layout,
    run_cli,
    stand_in_for_the_network,
    tunnel_init_args,
)
from tests.chatgpt.conftest import FakeMachine
from tests.chatgpt.fake_daemon import Daemon, running_daemon
from tests.fakes import write_executable

UNIT = "computer-chatgpt-tunnel-client.service"
PUBLIC_UNITS = ("computer-chatgpt-bridge.service", "computer-chatgpt-shim.service", "computer-chatgpt-tunnel.service")


def _init_tunnel(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, *extra: str) -> None:
    stand_in_for_the_network(monkeypatch)
    result = run_cli(capsys, *tunnel_init_args(), *extra)
    assert result.code == 0, result.err


def _store_key() -> None:
    write_key(layout().tunnel_key_file, RUNTIME_KEY)


def _use_health_port(port: int) -> None:
    save_settings(replace(load_settings(), tunnel_health_port=port))


def _unit_files() -> list[str]:
    unit_dir = layout().unit_dir
    return sorted(p.name for p in unit_dir.iterdir()) if unit_dir.exists() else []


# --- init -----------------------------------------------------------------------------------------------------------------------


def test_init_in_tunnel_mode_writes_one_unit_and_no_proxy_shim_or_cloudflared(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    result = run_cli(capsys, *tunnel_init_args())
    assert result.code == 0, result.err
    assert _unit_files() == [UNIT]
    assert sorted(p.name for p in layout().bin_dir.iterdir()) == ["tunnel-client"]
    assert not layout().password_file.exists()
    assert desktop_commander.launch_spec().entry.is_file()  # the shared core install, the one `computer mcp serve` runs
    assert "needs a domain + Cloudflare" in result.out  # the comparison is still shown
    settings = load_settings()
    assert settings.mode == "tunnel" and settings.tunnel_id == TUNNEL_ID and settings.tunnel_health_port > 0
    assert "login" in result.out and "not stored yet" in result.out


def test_the_unit_runs_the_pinned_client_with_the_key_as_a_file_and_never_contains_the_key(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    text = (layout().unit_dir / UNIT).read_text()
    exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert str(layout().tunnel_client_binary) in exec_line
    assert f"--control-plane.tunnel-id {TUNNEL_ID}" in exec_line
    assert f"--control-plane.api-key file:{layout().tunnel_key_file}" in exec_line
    assert "127.0.0.1:" in exec_line and "--mcp.command" in exec_line
    assert RUNTIME_KEY not in text
    assert "EnvironmentFile" not in text  # no secret reaches the daemon through the environment either


def test_init_twice_in_tunnel_mode_installs_and_rewrites_nothing(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    args = tunnel_init_args()
    run_cli(capsys, *args)
    again = run_cli(capsys, *args)
    assert "tunnel-client vTEST: already installed" in again.out and "(unchanged)" in again.out


def test_init_reports_a_stored_key_without_printing_it(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    again = run_cli(capsys, *tunnel_init_args())
    assert "runtime key: present" in again.out
    assert RUNTIME_KEY not in again.out + again.err


@pytest.mark.parametrize(
    "bad", ["", "tunnel_short", "TUNNEL_abcdefghijklmnopqrstuvwxyz012345", "tunnel_abcdefghijklmnopqrstuvwxyz0123456"]
)
def test_a_missing_or_malformed_tunnel_id_is_a_usage_error_and_saves_nothing(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, bad: str
) -> None:
    args = ["init", "--mode", "tunnel", "--lang", "en"] + (["--tunnel-id", bad] if bad else [])
    result = run_cli(capsys, *args)
    assert result.code == 2
    assert "tunnel id" in result.err
    assert not config_file().exists()


def test_a_tunnel_health_port_out_of_range_is_a_configuration_error(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine
) -> None:
    result = run_cli(capsys, *tunnel_init_args(), "--tunnel-health-port", "70000")
    assert result.code == 2
    assert "tunnel_health_port" in result.err


def test_the_health_port_defaults_to_8794() -> None:
    assert Settings().tunnel_health_port == 8794


def test_config_toml_keeps_its_strictness_for_the_new_settings() -> None:
    from agenttools_errors import ConfigError

    from computerlib.targets.chatgpt.settings import parse_settings

    with pytest.raises(ConfigError):
        parse_settings('tunnel_health_port = "8794"\n')
    with pytest.raises(ConfigError):
        parse_settings("tunnel_id = 5\n")
    assert parse_settings(f'mode = "tunnel"\ntunnel_id = "{TUNNEL_ID}"\n').tunnel_id == TUNNEL_ID


# --- the default never changes --------------------------------------------------------------------------------------------------


def test_a_plain_init_stays_public_even_when_the_config_holds_a_tunnel_id(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    save_settings(Settings(tunnel_id=TUNNEL_ID))
    result = run_cli(capsys, *init_args())
    assert result.code == 0, result.err
    assert load_settings().mode == "public"
    assert sorted(_unit_files()) == sorted(PUBLIC_UNITS[:2])


def test_a_saved_tunnel_mode_wins_over_the_default_and_a_flag_wins_over_the_saved_choice(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    assert run_cli(capsys, "init", "--lang", "en").code == 0  # no --mode: the saved choice stays
    assert load_settings().mode == "tunnel"
    # an explicit flag changes the mode, but not while the other mode's units are installed
    result = run_cli(capsys, "init", "--mode", "public", "--hostname", "dc.example.test", "--lang", "en")
    assert result.code == 2 and "uninstall" in result.err
    assert load_settings().mode == "tunnel"
    assert run_cli(capsys, "uninstall").code == 0
    assert run_cli(capsys, "init", "--mode", "public", "--hostname", "dc.example.test", "--lang", "en").code == 0
    assert load_settings().mode == "public"


def test_no_command_changes_the_mode_or_the_config(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    before = config_file().read_text()
    monkeypatch.setattr(lifecycle, "READY_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(sys, "stdin", io.StringIO(RUNTIME_KEY + "\n"))
    run_cli(capsys, "login", "--key-stdin")
    for command in (
        ["start"],
        ["status"],
        ["doctor"],
        ["restart"],
        ["stop"],
        ["logs"],
        ["connect", "--no-wait", "--no-open", "--no-copy"],
    ):
        run_cli(capsys, *command)
    run_cli(capsys, "end", "--revoke")
    assert config_file().read_text() == before
    assert load_settings().mode == "tunnel"


def test_public_mode_commands_do_not_know_the_tunnel_unit(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    run_cli(capsys, *init_args())
    run_cli(capsys, "start", "--no-wait")
    assert machine.calls() and all("tunnel-client" not in call for call in machine.calls())


def test_services_per_mode() -> None:
    assert processes.services(Settings()) == ["bridge", "shim"]
    assert processes.services(Settings(tunnel_name="t")) == ["bridge", "shim", "tunnel"]
    assert processes.services(Settings(mode="tunnel")) == ["tunnel-client"]
    layout_of = load_context(configured=False).layout
    built = processes.processes(Settings(mode="tunnel", tunnel_id=TUNNEL_ID), layout_of, ("computer",))
    assert [p.name for p in built] == ["tunnel-client"]


# --- login (tunnel mode: the runtime key) ---------------------------------------------------------------------------------------


def test_login_stores_the_key_from_stdin_in_a_0600_file_and_never_echoes_it(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"  {RUNTIME_KEY}  \n"))
    result = run_cli(capsys, "login", "--key-stdin")
    assert result.code == 0, result.err
    path = layout().tunnel_key_file
    assert path.read_text() == RUNTIME_KEY + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert RUNTIME_KEY not in result.out + result.err
    assert str(path) in result.out


def test_login_tightens_a_pre_existing_world_readable_key_file_before_writing(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    path = layout().tunnel_key_file
    path.write_text("old\n")
    path.chmod(0o644)
    monkeypatch.setattr(sys, "stdin", io.StringIO(RUNTIME_KEY + "\n"))
    assert run_cli(capsys, "login", "--key-stdin").code == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == RUNTIME_KEY + "\n"


def test_write_key_replaces_a_longer_old_key_completely(tmp_path: Path) -> None:
    path = tmp_path / "k"
    path.write_text("x" * 200 + "\n")
    path.chmod(0o666)
    write_key(path, "short")
    assert path.read_text() == "short\n" and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_login_without_a_terminal_or_the_flag_refuses_instead_of_reading_stdin(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(RUNTIME_KEY + "\n"))
    result = run_cli(capsys, "login")
    assert result.code == 2 and "--key-stdin" in result.err
    assert not layout().tunnel_key_file.exists()
    assert RUNTIME_KEY not in result.out + result.err


def test_login_asks_for_the_key_with_a_hidden_prompt_on_a_terminal(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    prompts: list[str] = []

    def hidden(prompt: str = "") -> str:
        prompts.append(prompt)
        return RUNTIME_KEY

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("computerlib.targets.chatgpt.commands.login.getpass.getpass", hidden)
    result = run_cli(capsys, "login")
    assert result.code == 0, result.err
    assert layout().tunnel_key_file.read_text() == RUNTIME_KEY + "\n"
    assert prompts and "hidden" in prompts[0]
    assert RUNTIME_KEY not in result.out + result.err + prompts[0]


def test_the_key_cannot_be_passed_as_an_argument(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    with pytest.raises(SystemExit):
        run_cli(capsys, "login", "--key", RUNTIME_KEY)
    assert not layout().tunnel_key_file.exists()


@pytest.mark.parametrize("bad", ["", "two words", "line\tbreak"])
def test_a_malformed_key_is_rejected_without_echoing_it(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    _init_tunnel(capsys, monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(bad + "\n"))
    result = run_cli(capsys, "login", "--key-stdin")
    assert result.code == 2
    assert bad.strip() == "" or bad not in result.out + result.err
    assert not layout().tunnel_key_file.exists()


def test_the_cloudflare_flags_are_refused_in_tunnel_mode_and_the_key_flag_in_public_mode(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    assert run_cli(capsys, "login", "--yes").code == 2
    stand_in_for_the_network(monkeypatch)
    run_cli(capsys, "uninstall")
    save_settings(Settings(hostname="dc.example.test"))
    assert run_cli(capsys, "login", "--key-stdin").code == 2


# --- start / stop / restart / end / uninstall -----------------------------------------------------------------------------------


def test_start_needs_the_stored_key_and_says_how_to_get_it(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    result = run_cli(capsys, "start", "--no-wait")
    assert result.code == 5
    assert "login --key-stdin" in result.err
    assert machine.unit_state(UNIT, "active") == ""


def test_start_enables_the_single_unit_and_waits_until_the_daemon_polls_the_control_plane(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    with running_daemon(Daemon()) as port:
        _use_health_port(port)
        result = run_cli(capsys, "start")
    assert result.code == 0, result.err
    assert "serving" in result.out and UNIT in result.out
    assert machine.unit_state(UNIT, "active") == "active" and machine.unit_state(UNIT, "enabled") == "enabled"
    assert all(machine.unit_state(name, "active") == "" for name in PUBLIC_UNITS)


def test_start_says_why_a_daemon_that_cannot_poll_the_control_plane_is_not_serving(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    monkeypatch.setattr(lifecycle, "READY_TIMEOUT_SECONDS", 0.3)
    with running_daemon(Daemon(polling=False, failures=2)) as port:
        _use_health_port(port)
        result = run_cli(capsys, "start")
    assert result.code == 3
    assert "awaiting_poll" in result.err and "key" in result.err
    assert RUNTIME_KEY not in result.out + result.err


def test_start_with_nothing_answering_on_the_health_port_names_the_state(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    monkeypatch.setattr(lifecycle, "READY_TIMEOUT_SECONDS", 0.3)
    result = run_cli(capsys, "start")
    assert result.code == 3 and "unavailable" in result.err


def test_start_refuses_a_hand_edited_malformed_tunnel_id(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    save_settings(replace(load_settings(), tunnel_id="tunnel_nope"))
    result = run_cli(capsys, "start", "--no-wait")
    assert result.code == 2 and "tunnel id" in result.err


def test_stop_restart_and_end_work_on_the_one_unit(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    run_cli(capsys, "start", "--no-wait")
    assert run_cli(capsys, "stop").code == 0
    assert machine.unit_state(UNIT, "active") == "inactive" and machine.unit_state(UNIT, "enabled") == "enabled"
    with running_daemon(Daemon()) as port:
        _use_health_port(port)
        assert run_cli(capsys, "restart").code == 0
    assert machine.unit_state(UNIT, "active") == "active"
    assert run_cli(capsys, "end").code == 0
    assert machine.unit_state(UNIT, "enabled") == "disabled"
    assert layout().tunnel_key_file.exists()  # end keeps the key, like the public end keeps the OAuth state


def test_end_revoke_deletes_the_stored_key_and_says_the_platform_key_must_be_revoked_too(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    result = run_cli(capsys, "end", "--revoke")
    assert result.code == 0, result.err
    assert not layout().tunnel_key_file.exists()
    assert "OpenAI Platform" in result.out
    assert RUNTIME_KEY not in result.out + result.err


def test_uninstall_removes_the_unit_and_keeps_the_key_until_purge_removes_everything_including_it(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    key = layout().tunnel_key_file
    assert run_cli(capsys, "uninstall").code == 0
    assert _unit_files() == [] and key.exists()
    _init_tunnel(capsys, monkeypatch)
    purged = run_cli(capsys, "uninstall", "--purge")
    assert purged.code == 0, purged.err
    assert not key.exists() and not config_file().exists()
    assert f"deleted {key}" in purged.out


def test_run_starts_the_client_in_the_foreground_with_the_key_file_and_no_key_in_the_argument_list(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    record = tmp_path / "argv.txt"
    layout().tunnel_client_binary.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\nexit 0\n')
    layout().tunnel_client_binary.chmod(0o755)
    result = run_cli(capsys, "run")
    assert result.code == 0, result.err
    argv = record.read_text().splitlines()
    assert f"file:{layout().tunnel_key_file}" in argv and TUNNEL_ID in argv
    assert RUNTIME_KEY not in "\n".join(argv)


def test_run_needs_the_key_and_the_installed_client(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    assert run_cli(capsys, "run").code == 5  # no key yet
    _store_key()
    layout().tunnel_client_binary.unlink()
    result = run_cli(capsys, "run")
    assert result.code == 5 and "tunnel-client" in result.err
    assert run_cli(capsys, "run", "--no-tunnel").code == 2


# --- status ---------------------------------------------------------------------------------------------------------------------


def test_status_is_unhealthy_with_exit_3_when_the_daemon_does_not_answer(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    result = run_cli(capsys, "status")
    assert result.code == 3
    assert "UNHEALTHY" in result.out and "unavailable" in result.out


def test_status_json_reports_the_state_and_the_counters_and_exits_0_when_healthy(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    run_cli(capsys, "start", "--no-wait")
    with running_daemon(Daemon(counts={"initialize": 1, "tools/list": 1, "tools/call": 2})) as port:
        _use_health_port(port)
        result = run_cli(capsys, "status", "--json")
        text = run_cli(capsys, "status")
    report = json.loads(result.out)
    assert result.code == 0 and text.code == 0
    assert report["mode"] == "tunnel" and report["healthy"] is True and report["state"] == "connected"
    assert (report["initialize"], report["tools_list"], report["tools_call"]) == (1, 1, 2)
    assert "cannot tell which OpenAI surface" in text.out
    assert RUNTIME_KEY not in result.out + text.out


@pytest.mark.parametrize(
    ("daemon", "state"),
    [(Daemon(ready=503), "not_ready"), (Daemon(polling=False, failures=4), "awaiting_poll")],
)
def test_status_is_unhealthy_while_the_daemon_is_not_ready_or_cannot_poll(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch, daemon: Daemon, state: str
) -> None:
    _init_tunnel(capsys, monkeypatch)
    run_cli(capsys, "start", "--no-wait")
    with running_daemon(daemon) as port:
        _use_health_port(port)
        result = run_cli(capsys, "status", "--json")
    assert result.code == 3 and json.loads(result.out)["state"] == state


def test_status_is_unhealthy_when_the_unit_is_inactive_even_if_something_answers_on_the_port(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    with running_daemon(Daemon()) as port:
        _use_health_port(port)
        assert run_cli(capsys, "status").code == 3


# --- doctor ---------------------------------------------------------------------------------------------------------------------


def _checks(capsys: pytest.CaptureFixture[str]) -> dict[str, dict[str, object]]:
    return {c["name"]: c for c in json.loads(run_cli(capsys, "doctor", "--json").out)}


def test_doctor_in_tunnel_mode_checks_the_tunnel_chain_and_nothing_public(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    run_cli(capsys, "start", "--no-wait")
    with running_daemon(Daemon()) as port:
        run_cli(capsys, *tunnel_init_args(), "--tunnel-health-port", str(port))  # the unit carries the port, so it is rewritten
        result = run_cli(capsys, "doctor")
    assert result.code == 0, result.out
    checks = _checks(capsys)
    assert {
        "tunnel id configured",
        "tunnel-client installed",
        "desktop-commander installed",
        "tunnel runtime key",
        "tunnel-client healthy",
    } <= set(checks)
    assert not any("proxy" in name or "shim" in name or "public URL" in name or "challenge" in name for name in checks)
    assert checks["tunnel runtime key"]["ok"]
    assert "PDF browser sandbox (write_pdf)" not in checks


def test_doctor_names_the_failure_of_each_link_and_how_to_fix_it_without_changing_anything(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    before = machine.calls()
    result = run_cli(capsys, "doctor")
    assert result.code == 3
    assert "[FAIL] tunnel runtime key" in result.out and "login --key-stdin" in result.out
    assert "[FAIL] tunnel-client healthy: unavailable" in result.out
    assert f"[FAIL] {UNIT} active" in result.out
    _store_key()
    layout().tunnel_key_file.chmod(0o644)
    assert "chmod 600" in run_cli(capsys, "doctor").out
    assert [c for c in machine.calls()[len(before) :] if not c.startswith("show ")] == []


def test_the_runtime_key_chmod_hint_pastes_safely_for_a_path_with_a_space_and_shell_syntax(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    odd = tmp_path / "my home $(touch pwned)"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(odd / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(odd / "data"))
    _init_tunnel(capsys, monkeypatch)
    _store_key()
    key = layout().tunnel_key_file
    key.chmod(0o644)
    out = run_cli(capsys, "doctor").out
    subprocess.run(["sh", "-c", "chmod 600 " + out.split("chmod 600 ", 1)[1].splitlines()[0]], cwd=tmp_path, check=True)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not (tmp_path / "pwned").exists()


def test_doctor_flags_a_tampered_client_binary_and_a_daemon_that_cannot_poll(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    layout().tunnel_client_binary.write_text("tampered")
    with running_daemon(Daemon(polling=False, failures=1)) as port:
        _use_health_port(port)
        checks = _checks(capsys)
    assert not checks["tunnel-client installed"]["ok"]
    assert not checks["tunnel-client healthy"]["ok"] and "awaiting_poll" in str(checks["tunnel-client healthy"]["detail"])


# --- connect --------------------------------------------------------------------------------------------------------------------


def test_connect_prints_the_tunnel_dialog_fields_and_the_honest_limits(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    with running_daemon(Daemon()) as port:
        _use_health_port(port)
        monkeypatch.setenv("PATH", str(machine.bin_dir))  # no clipboard tool, no browser opener
        result = run_cli(capsys, "connect", "--no-wait")
    assert result.code == 0, result.err
    for text in (
        "Add custom MCP server",
        "Tunnel",
        TUNNEL_ID,
        "None (No authentication)",
        "120 s per request",
        "one daemon per tunnel".lower(),
        "NOT verified here",
        "Enter tunnel ID instead",
        "Create as a plugin",
    ):
        assert text.lower() in result.out.lower(), text
    assert "Server URL" in result.out  # only to say it is NOT the choice
    assert RUNTIME_KEY not in result.out + result.err


def test_connect_needs_a_running_daemon(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    result = run_cli(capsys, "connect", "--no-wait", "--no-open", "--no-copy")
    assert result.code == 3
    assert "computer chatgpt start" in result.err


def test_connect_without_a_display_prints_the_settings_page_instead_of_running_xdg_open(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Over ssh xdg-open is installed but has no display to open on: connect must say where to go, not claim a browser opened.
    _init_tunnel(capsys, monkeypatch)
    opened = tmp_path / "xdg-open.log"
    write_executable(machine.bin_dir / "xdg-open", f'#!/bin/sh\necho "$@" >> "{opened}"\n')
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with running_daemon(Daemon()) as port:
        _use_health_port(port)
        monkeypatch.setenv("PATH", str(machine.bin_dir))
        result = run_cli(capsys, "connect", "--no-wait", "--no-copy")
    assert result.code == 0, result.err
    assert f"open {dialog.SETTINGS_PAGE} yourself" in result.out
    assert not opened.exists()


def test_connect_warns_when_the_daemon_cannot_poll_but_still_shows_the_fields(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    with running_daemon(Daemon(polling=False, failures=2)) as port:
        _use_health_port(port)
        monkeypatch.setenv("PATH", str(machine.bin_dir))
        result = run_cli(capsys, "connect", "--no-wait", "--no-open", "--no-copy")
    assert result.code == 0 and "warning" in result.out and "awaiting_poll" in result.out and TUNNEL_ID in result.out


def test_connect_with_a_wait_times_out_with_exit_3_and_does_not_claim_a_connection(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    monkeypatch.setattr("computerlib.targets.chatgpt.commands.connect.POLL_SECONDS", 0.05)
    with running_daemon(Daemon()) as port:
        _use_health_port(port)
        monkeypatch.setenv("PATH", str(machine.bin_dir))
        result = run_cli(capsys, "connect", "--no-open", "--no-copy", "--timeout", "0.3")
    assert result.code == 3
    assert "Not connected yet" in result.out and "awaiting_discovery" in result.out
    assert "Connected:" not in result.out


class Scripted:
    """A clock whose sleep applies the next scripted change to the stand-in daemon."""

    def __init__(self, steps: list[Callable[[], None]]) -> None:
        self.now = 0.0
        self._steps = steps

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self._steps:
            self._steps.pop(0)()

    def clock(self) -> float:
        return self.now


def test_the_watch_follows_the_states_in_order_ignores_earlier_traffic_and_stops_only_on_a_tool_call(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    daemon = Daemon(counts={"initialize": 4, "tools/list": 4, "tools/call": 9}, polling=False, failures=1)

    def poll_works() -> None:
        daemon.polling, daemon.failures = True, 0

    def discovery() -> None:
        daemon.counts["tools/list"] += 1

    def tool_call() -> None:
        daemon.counts["tools/call"] += 1

    script = Scripted([poll_works, discovery, tool_call])
    lines: list[str] = []
    with running_daemon(daemon) as port:
        _use_health_port(port)
        watch = watch_tunnel(load_context(), 600, out=lines.append, sleep=script.sleep, clock=script.clock)
    assert watch.connected
    text = "\n".join(lines)
    positions = [
        text.index(marker)
        for marker in ("cannot poll", "polling the control plane", "discovery request", "tools/call came through")
    ]
    assert positions == sorted(positions)
    assert script.now < 10


def test_the_watch_does_not_conclude_connected_from_old_calls_and_gives_up_at_the_timeout(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    daemon = Daemon(counts={"initialize": 4, "tools/list": 4, "tools/call": 9})
    script = Scripted([])
    with running_daemon(daemon) as port:
        _use_health_port(port)
        watch = watch_tunnel(load_context(), 5, out=lambda line: None, sleep=script.sleep, clock=script.clock)
    assert not watch.connected and watch.last is not None and watch.last.state == "awaiting_discovery"


def test_a_daemon_restart_during_the_watch_restarts_the_baseline_so_the_next_call_still_counts(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    daemon = Daemon(counts={"initialize": 4, "tools/list": 4, "tools/call": 9})

    def restarted_and_called() -> None:
        daemon.counts = {"initialize": 1, "tools/list": 1, "tools/call": 1}

    script = Scripted([restarted_and_called])
    with running_daemon(daemon) as port:
        _use_health_port(port)
        watch = watch_tunnel(load_context(), 600, out=lambda line: None, sleep=script.sleep, clock=script.clock)
    assert watch.connected


def test_a_stop_by_the_user_during_the_wait_exits_130(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)

    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("computerlib.targets.chatgpt.commands.connect.watch_tunnel", interrupted)
    with running_daemon(Daemon()) as port:
        _use_health_port(port)
        monkeypatch.setenv("PATH", str(machine.bin_dir))
        result = run_cli(capsys, "connect", "--no-open", "--no-copy")
    assert result.code == 130


def test_a_successful_connect_says_what_connected_means(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_tunnel(capsys, monkeypatch)
    monkeypatch.setattr("computerlib.targets.chatgpt.commands.connect.POLL_SECONDS", 0.05)
    daemon = Daemon()
    with running_daemon(daemon) as port:
        _use_health_port(port)
        monkeypatch.setenv("PATH", str(machine.bin_dir))
        call = threading.Timer(
            0.5, lambda: daemon.counts.update({"tools/call": 1})
        )  # after the baseline, as a real call would be
        call.start()
        try:
            result = run_cli(capsys, "connect", "--no-open", "--no-copy", "--timeout", "10")
        finally:
            call.cancel()
    assert result.code == 0, result.err + result.out
    assert "Connected: a tool call came through your tunnel" in result.out
    assert "does not by itself prove it was ChatGPT" in result.out
