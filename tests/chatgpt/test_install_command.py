"""`computer chatgpt install --hostname <name>`: the one-command setup, end to end against fakes.

The real command code runs; the machine boundary is faked: systemctl/loginctl/node/npm (conftest `machine`), the release downloads
(pinned stand-ins, the cloudflared one is the working fake of fake_cloudflared.py), the Cloudflare account (that fake's state
dir), DNS-over-HTTPS (a dict), the browser (a list) and the public URL (a function). Loopback HTTP servers stand in for the
started stack on scratch ports, because the fake systemctl starts nothing.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from computerlib import desktop_commander
from computerlib.cli import main
from computerlib.targets.chatgpt import cloudflare, zone
from computerlib.targets.chatgpt import machine as machine_identity
from computerlib.targets.chatgpt.commands import init as init_command
from computerlib.targets.chatgpt.commands import install as install_command
from computerlib.targets.chatgpt.context import Context
from computerlib.targets.chatgpt.password import read_password
from computerlib.targets.chatgpt.settings import config_file, load_settings
from tests.chatgpt import fake_cloudflared
from tests.chatgpt.cli_helpers import free_port, layout, run_cli, stand_in_for_the_network
from tests.chatgpt.conftest import FakeMachine
from tests.chatgpt.fake_cloudflared import FAKE_CLOUDFLARED, account_cert

HOST = "computer.example.test"
UNITS = ["computer-chatgpt-bridge.service", "computer-chatgpt-shim.service", "computer-chatgpt-tunnel.service"]
CLOUDFLARE_NS = {"Answer": [{"name": "example.test.", "type": 2, "data": "ada.ns.cloudflare.com."}]}


class _Answer401(BaseHTTPRequestHandler):
    def _reply(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Bearer resource_metadata="http://x/.well-known/oauth-protected-resource"')
        self.end_headers()

    do_GET = do_POST = _reply

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _answers(by_name: dict[str, object] | None) -> Callable[[str], object | None]:
    """A DNS-over-HTTPS stand-in: the NS answer per name (an empty NOERROR otherwise); None: every lookup fails."""

    def resolve(name: str) -> object | None:
        return None if by_name is None else by_name.get(name, {"Status": 0})

    return resolve


@contextmanager
def _serving(*ports: int) -> Generator[None]:
    """What the started units would do on their loopback ports: answer HTTP."""
    servers = [ThreadingHTTPServer(("127.0.0.1", port), _Answer401) for port in ports]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@dataclass
class World:
    account: Path
    opened: list[str]
    public: list[str]  # URLs the public check was asked about
    proxy_port: int
    shim_port: int
    metrics_port: int
    public_ok: bool = True

    def args(self, *extra: str) -> list[str]:
        return ["install", "--hostname", HOST, *self.common(), *extra]

    def common(self) -> list[str]:
        """Scratch ports and no wait for the public URL; every install of a test passes these."""
        ports = [
            "--proxy-port",
            str(self.proxy_port),
            "--shim-port",
            str(self.shim_port),
            "--metrics-port",
            str(self.metrics_port),
        ]
        return [*ports, "--timeout", "0"]


@pytest.fixture
def world(machine: FakeMachine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[World]:
    stand_in_for_the_network(monkeypatch, {"cloudflared": FAKE_CLOUDFLARED})
    account = tmp_path / "cloudflare-account"
    monkeypatch.setenv("FAKE_CF_STATE", str(account))
    monkeypatch.setattr(zone, "doh_ns", _answers({"example.test": CLOUDFLARE_NS}))
    found = World(account, [], [], free_port(), free_port(), free_port())

    def fake_open(url: str) -> bool:
        found.opened.append(url)
        return True

    def fake_public(base_url: str) -> tuple[bool, str]:
        found.public.append(base_url)
        return (True, "401 with the protected-resource challenge") if found.public_ok else (False, "no answer")

    monkeypatch.setattr(cloudflare, "open_in_browser", fake_open)
    monkeypatch.setattr(install_command, "challenge_ok", fake_public)
    real_start = install_command.start_stack
    with ExitStack() as running:
        serving: list[int] = []

        def start_and_serve(ctx: Context, **options: Any) -> None:
            """The fake systemctl starts nothing: the stand-in servers come up exactly when the units would."""
            if not serving:
                serving.extend((found.proxy_port, found.shim_port))
                running.enter_context(_serving(*serving))
            real_start(ctx, **options)

        monkeypatch.setattr(install_command, "start_stack", start_and_serve)
        yield found


def test_one_command_sets_up_everything(capsys: pytest.CaptureFixture[str], machine: FakeMachine, world: World) -> None:
    result = run_cli(capsys, *world.args())
    assert result.code == 0, result.err
    settings = load_settings()
    assert (settings.mode, settings.hostname, settings.tunnel_name) == ("public", HOST, "test-box")
    paths = layout()
    # pinned downloads and the shared Desktop Commander
    assert (paths.bin_dir / "mcp-auth-proxy").is_file() and (paths.bin_dir / "cloudflared").read_bytes() == FAKE_CLOUDFLARED
    assert desktop_commander.launch_spec().entry.is_file()
    # the password: generated, 0600, never printed
    password = read_password(paths.password_file)
    assert password and stat.S_IMODE(paths.password_file.stat().st_mode) == 0o600
    assert password not in result.out + result.err
    # units written, enabled, started; lingering on
    assert sorted(p.name for p in paths.unit_dir.iterdir()) == UNITS
    for unit in UNITS:
        assert machine.unit_state(unit, "enabled") == "enabled" and machine.unit_state(unit, "active") == "active"
    assert (machine.state_dir / "linger").exists()
    # Cloudflare: login in our own dir (browser opened by us), tunnel created, DNS routed; ~/.cloudflared untouched
    assert world.opened and fake_cloudflared.tunnels(world.account) == [settings.tunnel_name]
    assert fake_cloudflared.routes(world.account) == {HOST: settings.tunnel_name}
    assert (paths.cloudflared_dir / "cert.pem").is_file() and (paths.cloudflared_dir / f"{settings.tunnel_name}.json").is_file()
    assert not (Path(os.environ["HOME"]) / ".cloudflared").exists()
    # the public URL was waited for, then ChatGPT's fields printed
    assert world.public == [f"https://{HOST}"]
    for text in (f"https://{HOST}/mcp", "Add custom MCP server", "OAuth", str(paths.password_file), "ready:"):
        assert text in result.out, text


def test_running_install_again_changes_nothing(capsys: pytest.CaptureFixture[str], machine: FakeMachine, world: World) -> None:
    assert run_cli(capsys, *world.args()).code == 0
    password = read_password(layout().password_file)
    calls_before, cloudflared_before = len(machine.calls()), len(fake_cloudflared.calls(world.account))
    again = run_cli(capsys, *world.args())
    assert again.code == 0, again.err
    assert read_password(layout().password_file) == password
    assert "(unchanged)" in again.out and "already installed" in again.out and "already routed" in again.out
    new_cloudflared = [" ".join(c.argv) for c in fake_cloudflared.calls(world.account)[cloudflared_before:]]
    assert not any(" login" in c or " create " in c or "--overwrite-dns" in c for c in new_cloudflared)
    new_systemctl = machine.calls()[calls_before:]
    assert not any(c.startswith(("restart", "daemon-reload")) for c in new_systemctl)


def test_a_changed_setting_rewrites_and_restarts_only_the_changed_units(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, world: World
) -> None:
    assert run_cli(capsys, *world.args()).code == 0
    calls_before = len(machine.calls())
    world.metrics_port = free_port()
    assert run_cli(capsys, *world.args()).code == 0
    restarts = [c for c in machine.calls()[calls_before:] if c.startswith("restart")]
    assert restarts == ["restart computer-chatgpt-tunnel.service"]


def test_a_hostname_outside_cloudflare_stops_before_anything_is_written(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = {"Answer": [{"name": "example.test.", "type": 2, "data": "ns1.registrar.test."}]}
    monkeypatch.setattr(zone, "doh_ns", _answers({"example.test": elsewhere}))
    result = run_cli(capsys, *world.args())
    assert result.code == 2
    assert "not in a zone on Cloudflare" in result.err and "ns1.registrar.test" in result.err
    assert not config_file().exists() and not layout().unit_dir.exists()
    assert machine.calls() == ["show-environment"]


def test_a_failed_zone_lookup_only_warns(
    capsys: pytest.CaptureFixture[str], world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(zone, "doh_ns", _answers(None))
    result = run_cli(capsys, *world.args())
    assert result.code == 0, result.err
    assert "could not look up the nameservers" in result.out


def test_without_node_it_stops_with_127_before_anything_is_written(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, world: World
) -> None:
    (machine.bin_dir / "node").unlink()
    os.environ["PATH"] = str(
        machine.bin_dir
    )  # only the fakes: a host node must not satisfy the check (monkeypatch restores PATH)
    result = run_cli(capsys, *world.args())
    assert result.code == 127
    assert "`node` is not installed" in result.err and "sudo apt install nodejs" in result.err
    assert not config_file().exists()


def test_a_port_held_by_another_process_is_refused(capsys: pytest.CaptureFixture[str], world: World) -> None:
    with _serving(world.metrics_port):
        result = run_cli(capsys, *world.args())
    assert result.code == 2
    assert f"127.0.0.1:{world.metrics_port} is already in use" in result.err and "--metrics-port" in result.err
    assert not config_file().exists()


def test_an_existing_dns_record_is_not_replaced_without_yes(capsys: pytest.CaptureFixture[str], world: World) -> None:
    fake_cloudflared.existing_record(world.account, HOST, "live-tunnel")
    result = run_cli(capsys, *world.args())
    assert result.code == 2
    assert "--yes" in result.err
    assert fake_cloudflared.routes(world.account) == {HOST: "live-tunnel"}
    replaced = run_cli(capsys, *world.args("--yes"))
    assert replaced.code == 0, replaced.err
    assert fake_cloudflared.routes(world.account) == {HOST: load_settings().tunnel_name}


def test_a_public_url_that_never_answers_exits_7_after_printing_the_fields(
    capsys: pytest.CaptureFixture[str], world: World
) -> None:
    world.public_ok = False
    result = run_cli(capsys, *world.args())
    assert result.code == 7
    assert f"https://{HOST}/mcp" in result.out and "Add custom MCP server" in result.out
    assert "computer chatgpt doctor" in result.err


def test_an_invalid_hostname_is_a_usage_error(capsys: pytest.CaptureFixture[str], world: World) -> None:
    result = run_cli(capsys, "install", "--hostname", "not a host")
    assert result.code == 2
    assert "not a public hostname" in result.err
    assert not config_file().exists()


def test_without_a_hostname_it_says_what_to_pass(capsys: pytest.CaptureFixture[str], world: World) -> None:
    result = run_cli(capsys, "install")
    assert result.code == 2
    assert "--hostname" in result.err


def test_the_tunnel_name_and_cloudflared_dir_can_be_chosen(
    capsys: pytest.CaptureFixture[str], world: World, tmp_path: Path
) -> None:
    own = tmp_path / "my-cloudflared"
    result = run_cli(capsys, *world.args("--tunnel-name", "laptop", "--cloudflared-dir", str(own)))
    assert result.code == 0, result.err
    assert fake_cloudflared.routes(world.account) == {HOST: "laptop"}
    assert (own / "cert.pem").is_file() and (own / "laptop.json").is_file()
    tunnel_unit = (layout().unit_dir / "computer-chatgpt-tunnel.service").read_text()
    assert f"--credentials-file {own}/laptop.json" in tunnel_unit


def test_help_lists_install(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "chatgpt" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["chatgpt", "--help"])
    assert "install" in capsys.readouterr().out


def test_the_saved_config_is_plain_toml_without_secrets(capsys: pytest.CaptureFixture[str], world: World) -> None:
    assert run_cli(capsys, *world.args()).code == 0
    text = config_file().read_text()
    assert read_password(layout().password_file) not in text
    assert json.dumps(HOST) in text


MACHINE_HOST = "test-box-mcp.example.test"


def _doctor_failures(capsys: pytest.CaptureFixture[str]) -> list[tuple[str, str]]:
    """The local checks `doctor` fails, with their fix: what it would tell the user to do."""
    result = run_cli(capsys, "doctor", "--local", "--json")
    return [(check["name"], check["fix"]) for check in json.loads(result.out) if not check["ok"]]


def test_domain_names_the_hostname_and_the_tunnel_after_the_machine(capsys: pytest.CaptureFixture[str], world: World) -> None:
    result = run_cli(capsys, "install", "--domain", "example.test", *world.common())
    assert result.code == 0, result.err
    settings = load_settings()
    assert (settings.hostname, settings.domain, settings.machine_name, settings.tunnel_name) == (
        MACHINE_HOST,
        "example.test",
        "test-box",
        "test-box",
    )
    assert fake_cloudflared.tunnels(world.account) == ["test-box"]
    assert fake_cloudflared.routes(world.account) == {MACHINE_HOST: "test-box"}
    assert world.public == [f"https://{MACHINE_HOST}"]
    # the ChatGPT connector is named after the machine, so several machines stay apart
    name_line = next(line for line in result.out.splitlines() if line.strip().startswith("Name "))
    assert name_line.split()[1] == "test-box"


def test_a_re_run_without_flags_reuses_the_saved_domain_and_machine_name(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, world: World
) -> None:
    assert run_cli(capsys, "install", "--domain", "example.test", *world.common()).code == 0
    saved = config_file().read_text()
    cloudflared_before = len(fake_cloudflared.calls(world.account))
    again = run_cli(capsys, "install", "--timeout", "0")
    assert again.code == 0, again.err
    assert config_file().read_text() == saved
    new = [" ".join(c.argv) for c in fake_cloudflared.calls(world.account)[cloudflared_before:]]
    assert not any(" login" in c or " create " in c or " token " in c or "--overwrite-dns" in c for c in new)
    assert "tunnel test-box: exists" in again.out and "already routed" in again.out


def test_a_tunnel_of_the_same_name_without_local_credentials_is_refused(capsys: pytest.CaptureFixture[str], world: World) -> None:
    """Another machine with the same hostname already runs `test-box`: never take it over."""
    world.account.mkdir(parents=True, exist_ok=True)
    (world.account / "tunnels.json").write_text(json.dumps(["test-box"]))
    result = run_cli(capsys, "install", "--domain", "example.test", *world.common())
    assert result.code == 2
    assert "already exists on the account" in result.err and "--machine-name" in result.err
    assert not (layout().cloudflared_dir / "test-box.json").exists()
    assert not any(" token " in " ".join(c.argv) for c in fake_cloudflared.calls(world.account))
    assert fake_cloudflared.routes(world.account) == {}
    # nothing is saved: a config naming the other machine's tunnel would send `uninstall` and `connect` to it
    assert not config_file().exists()
    renamed = run_cli(capsys, "install", "--domain", "example.test", "--machine-name", "test-box-2", *world.common())
    assert renamed.code == 0, renamed.err
    assert fake_cloudflared.routes(world.account) == {"test-box-2-mcp.example.test": "test-box-2"}
    assert load_settings().machine_name == "test-box-2"


def test_uninstall_purge_cleans_up_after_a_refused_first_install(capsys: pytest.CaptureFixture[str], world: World) -> None:
    """The refused first install saved no config but stored the account certificate: --purge must still remove it."""
    world.account.mkdir(parents=True, exist_ok=True)
    (world.account / "tunnels.json").write_text(json.dumps(["test-box"]))
    assert run_cli(capsys, "install", "--domain", "example.test", *world.common()).code == 2
    assert (layout().cloudflared_dir / "cert.pem").exists() and not config_file().exists()
    # a tunnel-mode unit whose config was lost: without the config the mode is unknown, so purge must stop it too
    tunnel_client = layout().unit_dir / "computer-chatgpt-tunnel-client.service"
    tunnel_client.parent.mkdir(parents=True, exist_ok=True)
    tunnel_client.write_text("[Service]\nExecStart=/bin/true\n")
    purged = run_cli(capsys, "uninstall", "--purge")
    assert purged.code == 0, purged.err
    assert not (layout().cloudflared_dir / "cert.pem").exists() and not tunnel_client.exists()
    assert "computer-chatgpt-tunnel-client.service" in purged.out


def test_a_taken_tunnel_on_a_hostname_config_points_at_tunnel_name(capsys: pytest.CaptureFixture[str], world: World) -> None:
    """With a fixed hostname --machine-name never moves the tunnel, so suggesting it would loop on the same refusal."""
    world.account.mkdir(parents=True, exist_ok=True)
    (world.account / "tunnels.json").write_text(json.dumps(["test-box"]))
    result = run_cli(capsys, *world.args())
    assert result.code == 2 and "--tunnel-name test-box" in result.err and "--machine-name" not in result.err


def test_a_refused_rename_leaves_the_units_so_the_corrected_run_restarts_the_running_stack(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, world: World
) -> None:
    """A run that stops on the same-name refusal must not rewrite the units: the corrected re-run would find them unchanged and
    leave the running services on the old tunnel and hostname."""
    assert run_cli(capsys, "install", "--domain", "example.test", *world.common()).code == 0
    unit_dir = layout().unit_dir
    units_before = {path.name: path.read_text() for path in unit_dir.iterdir()}
    config_before = config_file().read_text()
    doctor_before = _doctor_failures(capsys)
    (world.account / "tunnels.json").write_text(json.dumps([*fake_cloudflared.tunnels(world.account), "taken"]))
    refused = run_cli(capsys, "install", "--machine-name", "taken", "--timeout", "0")
    assert refused.code == 2 and "already exists on the account" in refused.err
    assert {path.name: path.read_text() for path in unit_dir.iterdir()} == units_before
    # connect, doctor and uninstall read the config: it still names this machine's running tunnel and URL, not `taken`'s
    assert config_file().read_text() == config_before
    assert _doctor_failures(capsys) == doctor_before  # no "stale units: run init", which would write units for `taken`
    calls_before = len(machine.calls())
    corrected = run_cli(capsys, "install", "--machine-name", "test-box-3", "--timeout", "0")
    assert corrected.code == 0, corrected.err
    restarts = [c for c in machine.calls()[calls_before:] if c.startswith("restart")]
    # the new hostname and tunnel are in the bridge (external URL), shim and tunnel units: all three were running and must restart
    assert restarts == ["restart computer-chatgpt-bridge.service computer-chatgpt-shim.service computer-chatgpt-tunnel.service"]
    assert fake_cloudflared.routes(world.account)["test-box-3-mcp.example.test"] == "test-box-3"


def test_a_domain_below_the_zone_is_refused_before_anything_is_written(capsys: pytest.CaptureFixture[str], world: World) -> None:
    result = run_cli(capsys, "install", "--domain", "sub.example.test", *world.common())
    assert result.code == 2
    assert "--domain example.test" in result.err and "Universal SSL" in result.err
    assert not config_file().exists()


def test_a_uuid_system_hostname_is_refused_before_anything_is_written(
    capsys: pytest.CaptureFixture[str], world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It would become the tunnel name, which cloudflared refuses only after downloads and login."""
    monkeypatch.setattr(machine_identity, "system_hostname", lambda: "550e8400-e29b-41d4-a716-446655440000")
    result = run_cli(capsys, "install", "--domain", "example.test", *world.common())
    assert result.code == 2 and "--machine-name" in result.err
    assert not config_file().exists()
    assert fake_cloudflared.calls(world.account) == []


def test_a_bad_cloudflare_cert_is_refused_before_anything_is_written(
    capsys: pytest.CaptureFixture[str], world: World, tmp_path: Path
) -> None:
    bad = tmp_path / "cert.pem"
    bad.write_text("not a certificate\n")
    for path in (tmp_path / "typo.pem", bad):
        result = run_cli(capsys, "install", "--domain", "example.test", "--cloudflare-cert", str(path), *world.common())
        assert result.code != 0 and str(path) in result.err
        assert not config_file().exists() and not layout().bin_dir.exists()
        assert fake_cloudflared.calls(world.account) == []


def test_the_account_certificate_can_come_from_a_pipe(capsys: pytest.CaptureFixture[str], world: World) -> None:
    """The README streams it with `<(ssh … cat cert.pem)`: a pipe can be read once only, so it must be read exactly once."""
    read_end, write_end = os.pipe()
    with os.fdopen(write_end, "w") as writer:
        writer.write(account_cert("a"))
    try:
        cert = f"/dev/fd/{read_end}"
        result = run_cli(capsys, "install", "--domain", "example.test", "--cloudflare-cert", cert, *world.common())
    finally:
        os.close(read_end)
    assert result.code == 0, result.err
    assert (layout().cloudflared_dir / "cert.pem").read_text() == account_cert("a")


def test_machine_name_on_a_hostname_config_never_moves_its_tunnel_or_dns(
    capsys: pytest.CaptureFixture[str], world: World
) -> None:
    """The cutover layout (a fixed --hostname served by tunnel `dc-local`, no saved machine name): re-running install with
    --machine-name --yes only names the connector. It never creates a tunnel, never re-points the record, never orphans one."""
    first = run_cli(capsys, *world.args("--tunnel-name", "dc-local"))
    assert first.code == 0, first.err
    text = config_file().read_text()
    config_file().write_text("\n".join(line for line in text.splitlines() if not line.startswith("machine_name")) + "\n")
    assert load_settings().machine_name == ""
    cloudflared_before = len(fake_cloudflared.calls(world.account))
    for name in ("ubuntu-pc", "ubuntu-pc", "laptop"):
        again = run_cli(capsys, "install", "--machine-name", name, "--yes", "--timeout", "0")
        assert again.code == 0, again.err
        assert fake_cloudflared.tunnels(world.account) == ["dc-local"]
        assert fake_cloudflared.routes(world.account) == {HOST: "dc-local"}
        settings = load_settings()
        assert (settings.hostname, settings.tunnel_name, settings.machine_name) == (HOST, "dc-local", name)
    new = [" ".join(c.argv) for c in fake_cloudflared.calls(world.account)[cloudflared_before:]]
    assert not any(" create " in c or "--overwrite-dns" in c or " login" in c for c in new)


def test_an_existing_account_certificate_replaces_the_browser_login(
    capsys: pytest.CaptureFixture[str], world: World, tmp_path: Path
) -> None:
    cert = tmp_path / "from-the-first-machine" / "cert.pem"
    cert.parent.mkdir()
    cert.write_text(account_cert("a"))
    result = run_cli(capsys, "install", "--domain", "example.test", "--cloudflare-cert", str(cert), *world.common())
    assert result.code == 0, result.err
    copied = layout().cloudflared_dir / "cert.pem"
    assert copied.read_text() == cert.read_text() and stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert world.opened == []
    assert not any(c.argv[:2] == ["tunnel", "login"] for c in fake_cloudflared.calls(world.account))


def test_a_cpu_without_pinned_binaries_is_refused_before_anything_is_written(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_pin(name: str, machine: str | None = None) -> None:
        """A CPU the pin table does not cover (armv7l, for one)."""
        del name, machine

    monkeypatch.setattr(init_command, "binary_pin", no_pin)
    result = run_cli(capsys, "install", "--domain", "example.test", *world.common())
    assert result.code == 2
    assert "no pinned mcp-auth-proxy for this CPU" in result.err and "aarch64" in result.err
    assert not config_file().exists() and machine.calls() == []
