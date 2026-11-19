"""The commands as a user runs them: init, start, stop, end, status, doctor, logs and connect against a fake systemd and local
servers."""

from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from computerlib import desktop_commander
from computerlib.targets.chatgpt import lifecycle
from computerlib.targets.chatgpt.settings import config_file, load_settings, save_settings
from tests.chatgpt.cli_helpers import init_args, layout, run_cli, stand_in_for_the_network
from tests.chatgpt.conftest import FakeMachine
from tests.chatgpt.shim_harness import Running, jwt, running_shim

OPENAI_UA = "openai-mcp/1.0.0"


def test_init_shows_the_comparison_table_and_keeps_the_public_default(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    result = run_cli(capsys, *init_args())
    assert result.code == 0
    assert "needs a domain + Cloudflare" in result.out
    assert "Default: public" in result.out
    assert load_settings().mode == "public"


def test_init_in_russian_shows_the_russian_table(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    result = run_cli(capsys, "init", "--hostname", "dc.example.test", "--lang", "ru")
    assert "нужны домен и Cloudflare" in result.out


def test_init_never_prints_the_password_and_keeps_its_file_private(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    result = run_cli(capsys, *init_args())
    assert result.code == 0, result.out + result.err
    password_file = layout().password_file
    secret = password_file.read_text().split("=", 1)[1].strip()
    assert secret and secret not in result.out + result.err
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600


def test_init_twice_keeps_the_password_and_rewrites_no_unit(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    args = init_args()
    run_cli(capsys, *args)
    first = layout().password_file.read_text()
    second_run = run_cli(capsys, *args)
    assert layout().password_file.read_text() == first
    assert "kept" in second_run.out and "already installed" in second_run.out and "(unchanged)" in second_run.out


def test_init_installs_the_pinned_binary_and_desktop_commander(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    run_cli(capsys, *init_args())
    assert (layout().bin_dir / "mcp-auth-proxy").is_file()
    assert desktop_commander.launch_spec().entry.is_file()  # the shared core install, the one `computer mcp serve` runs


def test_init_without_a_hostname_explains_what_to_pass(capsys: pytest.CaptureFixture[str], machine: FakeMachine) -> None:
    result = run_cli(capsys, "init", "--lang", "en")
    assert result.code == 2
    assert "--hostname" in result.err
    assert not config_file().exists()


def test_the_tunnel_mode_without_a_tunnel_id_explains_what_to_pass_and_saves_nothing(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine
) -> None:
    result = run_cli(capsys, "init", "--mode", "tunnel", "--lang", "en")
    assert result.code == 2
    assert "--tunnel-id" in result.err
    assert not config_file().exists()


def test_commands_before_init_point_at_init(capsys: pytest.CaptureFixture[str], machine: FakeMachine) -> None:
    result = run_cli(capsys, "start")
    assert result.code == 2
    assert "computer chatgpt init" in result.err


def _init(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, shim: Running | None = None) -> None:
    stand_in_for_the_network(monkeypatch)
    init = run_cli(capsys, *init_args())
    assert init.code == 0, init.out + init.err
    if shim is not None:
        save_settings(replace(load_settings(), shim_port=shim.port, proxy_port=shim.upstream_port))


def test_start_enables_and_starts_every_unit_and_waits_until_it_serves(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with running_shim(tmp_path_factory.mktemp("shim")) as shim:
        _init(capsys, monkeypatch, shim)
        result = run_cli(capsys, "start")
    assert result.code == 0, result.err
    assert "serving" in result.out
    for name in ("computer-chatgpt-bridge.service", "computer-chatgpt-shim.service"):
        assert machine.unit_state(name, "active") == "active"
        assert machine.unit_state(name, "enabled") == "enabled"
    assert machine.unit_state("", "linger") == "" and (machine.state_dir / "linger").exists()


def test_start_reports_a_stack_that_never_serves(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    monkeypatch.setattr(lifecycle, "READY_TIMEOUT_SECONDS", 0.3)
    result = run_cli(capsys, "start")
    assert result.code == 3
    assert "did not start serving" in result.err


def test_stop_stops_the_units_but_leaves_them_enabled(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    run_cli(capsys, "start", "--no-wait")
    assert run_cli(capsys, "stop").code == 0
    assert machine.unit_state("computer-chatgpt-shim.service", "active") == "inactive"
    assert machine.unit_state("computer-chatgpt-shim.service", "enabled") == "enabled"


def test_end_disables_the_units_and_keeps_the_oauth_state_unless_asked(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    state = layout().data_dir
    state.mkdir(parents=True)
    (state / "db").write_text("clients")
    run_cli(capsys, "start", "--no-wait")
    assert run_cli(capsys, "end").code == 0
    assert machine.unit_state("computer-chatgpt-shim.service", "enabled") == "disabled"
    assert (state / "db").exists()
    assert run_cli(capsys, "end", "--revoke").code == 0
    assert not state.exists()


def test_status_is_unhealthy_with_exit_3_when_nothing_runs(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    result = run_cli(capsys, "status")
    assert result.code == 3
    assert "UNHEALTHY" in result.out


def test_status_json_is_healthy_and_counts_only_verified_chatgpt_tool_calls(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with running_shim(tmp_path_factory.mktemp("shim")) as shim:
        _init(capsys, monkeypatch, shim)
        shim.upstream.routes["/mcp"] = (200, {}, b"{}")
        call = b'{"method":"tools/call","params":{"name":"get_config"}}'
        shim.request("POST", "/mcp", headers={"Authorization": f"Bearer {jwt('x')}", "User-Agent": OPENAI_UA}, body=call)
        shim.log_lines()
        shim_log = layout().access_log
        shim_log.parent.mkdir(parents=True, exist_ok=True)
        shim_log.write_text(
            "2026-11-08T10:00:00.000Z 200 POST /mcp 3ms client=openai-mcp rpc=tools/call tool=get_config\n"
            "2026-11-08T10:00:01.000Z 200 POST /mcp 3ms client=unverified rpc=tools/call tool=start_process\n"
        )
        run_cli(capsys, "start", "--no-wait")
        result = run_cli(capsys, "status", "--json")
    report = json.loads(result.out)
    assert result.code == 0, result.out
    assert report["healthy"] is True
    assert report["chatgpt_tool_calls"] == 1
    assert report["last_chatgpt_tool_call"] == "2026-11-08T10:00:00.000Z"
    assert report["mode"] == "public"


def test_doctor_names_what_is_wrong_and_how_to_fix_it_without_changing_anything(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    before = machine.calls()
    layout().password_file.chmod(0o644)
    result = run_cli(capsys, "doctor", "--local")
    assert result.code == 3
    assert "[FAIL] login password" in result.out and "chmod 600" in result.out
    assert "[FAIL] computer-chatgpt-bridge.service active" in result.out
    assert "run `computer chatgpt start`" in result.out
    assert [c for c in machine.calls()[len(before) :] if not c.startswith("show ")] == []
    assert not (layout().state_dir / "heal.json").exists()  # the watchdog state is --heal's alone


def test_doctor_fails_a_checksum_correct_binary_without_the_exec_bit_and_init_repairs_it(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    binary = layout().bin_dir / "mcp-auth-proxy"
    binary.chmod(0o644)  # a backup restore keeps the bytes but drops the mode; systemd cannot exec it
    result = run_cli(capsys, "doctor", "--local")
    assert "[FAIL] mcp-auth-proxy installed" in result.out and "is not executable" in result.out
    assert f"run `computer chatgpt init` (or chmod 755 {binary})" in result.out
    run_cli(capsys, *init_args())
    assert stat.S_IMODE(binary.stat().st_mode) == 0o755
    assert "[ok] mcp-auth-proxy installed" in run_cli(capsys, "doctor", "--local").out


def test_doctors_chmod_hints_paste_safely_for_a_path_with_a_space_and_shell_syntax(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    odd = tmp_path / "my home $(touch pwned)"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(odd / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(odd / "data"))
    _init(capsys, monkeypatch)
    password, binary = layout().password_file, layout().bin_dir / "mcp-auth-proxy"
    password.chmod(0o644)
    binary.chmod(0o644)
    out = run_cli(capsys, "doctor", "--local").out
    hints = [
        "chmod 600 " + out.split("chmod 600 ", 1)[1].splitlines()[0],
        "chmod 755 " + out.split("chmod 755 ", 1)[1].splitlines()[0].removesuffix(")"),
    ]
    for hint in hints:  # pasted into a shell as printed
        subprocess.run(["sh", "-c", hint], cwd=tmp_path, check=True)
    assert stat.S_IMODE(password.stat().st_mode) == 0o600
    assert stat.S_IMODE(binary.stat().st_mode) == 0o755
    assert not (tmp_path / "pwned").exists()


def test_doctor_points_each_local_probe_at_the_unit_that_runs_it(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    save_settings(replace(load_settings(), unit_prefix="dc-mcp"))
    checks = {c["name"]: c for c in json.loads(run_cli(capsys, "doctor", "--local", "--json").out)}
    # The proxy runs inside the bridge unit; there is no dc-mcp-proxy.service to read.
    assert not checks["proxy answers"]["ok"]
    assert checks["proxy answers"]["fix"] == "journalctl --user -u dc-mcp-bridge.service"
    assert not checks["shim answers"]["ok"]
    assert checks["shim answers"]["fix"] == "journalctl --user -u dc-mcp-shim.service"


def test_doctor_json_lists_every_check(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    checks = json.loads(run_cli(capsys, "doctor", "--local", "--json").out)
    names = {c["name"] for c in checks}
    assert {"mcp-auth-proxy installed", "desktop-commander installed", "login password", "unit files current"} <= names
    assert next(c for c in checks if c["name"] == "unit files current")["ok"]


def test_doctor_passes_the_challenge_check_against_a_serving_shim(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with running_shim(tmp_path_factory.mktemp("shim")) as shim:
        _init(capsys, monkeypatch, shim)
        shim.upstream.routes["/mcp"] = (401, {}, b"{}")
        run_cli(capsys, "start", "--no-wait")
        checks = json.loads(run_cli(capsys, "doctor", "--local", "--json").out)
    challenge = next(c for c in checks if c["name"] == "401 challenge through the shim")
    assert challenge["ok"], challenge


def test_logs_filters_by_tool_client_and_oauth_step(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    log = layout().access_log
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "2026-11-08T10:00:00.000Z 201 POST /.idp/register 1ms client=unverified\n"
        "2026-11-08T10:00:01.000Z 200 POST /mcp 3ms client=openai-mcp rpc=tools/call tool=get_config\n"
        "2026-11-08T10:00:02.000Z 200 POST /mcp 3ms client=unverified rpc=tools/call tool=start_process\n"
        "garbage that is not a log line\n"
    )
    assert run_cli(capsys, "logs", "--tool", "get_config").out.count("\n") == 1
    assert "start_process" in run_cli(capsys, "logs", "--client", "unverified", "--tool", "start_process").out
    assert "/.idp/register" in run_cli(capsys, "logs", "--oauth").out
    assert run_cli(capsys, "logs", "-n", "1").out.strip().endswith("tool=start_process")


def test_logs_before_any_traffic_says_so_with_exit_5(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    result = run_cli(capsys, "logs")
    assert result.code == 5
    assert "no log yet" in result.out


def test_connect_prints_every_dialog_field_but_never_the_password(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with running_shim(tmp_path_factory.mktemp("shim")) as shim:
        _init(capsys, monkeypatch, shim)
        monkeypatch.setenv("PATH", str(machine.bin_dir))  # no clipboard tool, no browser opener
        result = run_cli(capsys, "connect", "--no-wait")
    secret = layout().password_file.read_text().split("=", 1)[1].strip()
    assert result.code == 0, result.err
    for text in (
        "Add custom MCP server",
        "https://dc.example.test/mcp",
        "Authentication",
        "OAuth",
        "I understand and want to continue",
        "Create as a plugin",
    ):
        assert text in result.out
    assert str(layout().password_file) in result.out
    assert secret not in result.out + result.err


def test_connect_needs_a_running_stack(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch)
    result = run_cli(capsys, "connect", "--no-wait", "--no-open", "--no-copy")
    assert result.code == 3
    assert "computer chatgpt start" in result.err
