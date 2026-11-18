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
from computerlib.targets.chatgpt.commands import install as install_command
from computerlib.targets.chatgpt.context import Context
from computerlib.targets.chatgpt.password import read_password
from computerlib.targets.chatgpt.settings import config_file, load_settings
from tests.chatgpt import fake_cloudflared
from tests.chatgpt.cli_helpers import free_port, layout, run_cli, stand_in_for_the_network
from tests.chatgpt.conftest import FakeMachine
from tests.chatgpt.fake_cloudflared import FAKE_CLOUDFLARED

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
        ports = [
            "--proxy-port",
            str(self.proxy_port),
            "--shim-port",
            str(self.shim_port),
            "--metrics-port",
            str(self.metrics_port),
        ]
        return ["install", "--hostname", HOST, *ports, "--timeout", "0", *extra]


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
    assert (settings.mode, settings.hostname, settings.tunnel_name) == ("public", HOST, "computer-computer-example-test")
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
