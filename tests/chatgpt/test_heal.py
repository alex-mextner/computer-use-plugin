"""`computer chatgpt doctor --heal` against the fake systemctl and real loopback servers: a hung server is a socket that accepts
connections and never answers (what a SIGSTOPped proxy looks like from outside); the internet is a local listener or a closed
port."""

from __future__ import annotations

import fcntl
import http.server
import socket
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from computerlib.targets.chatgpt import heal, watchdog
from computerlib.targets.chatgpt.context import Context
from computerlib.targets.chatgpt.settings import load_settings, save_settings
from tests.chatgpt.cli_helpers import (
    Result,
    free_port,
    init_args,
    layout,
    run_cli,
    stand_in_for_the_network,
    tunnel_init_args,
)
from tests.chatgpt.conftest import FakeMachine

BRIDGE = "computer-chatgpt-bridge.service"
SHIM = "computer-chatgpt-shim.service"
TUNNEL = "computer-chatgpt-tunnel.service"


@contextmanager
def hung_server(port: int) -> Generator[None]:
    """Listens, so the kernel completes every connection, and never reads or answers."""
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(64)
        yield


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = 503 if self.path == "/ready" else 404
        body = b'{"status":503,"readyConnections":0}' if status == 503 else b"not found"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def serving(port: int) -> Generator[None]:
    """Answers every request: 404 (alive) everywhere, and /ready with cloudflared's 503 for zero edge connections."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    ticking = Clock()
    monkeypatch.setattr(heal, "wall_clock", ticking)
    monkeypatch.setattr(heal, "PROBE_TIMEOUT_SECONDS", 0.2)
    return ticking


@pytest.fixture
def internet(monkeypatch: pytest.MonkeyPatch) -> Iterator[socket.socket]:
    """The connectivity anchor is a local listener: online while it listens."""
    with socket.socket() as anchor:
        anchor.bind(("127.0.0.1", 0))
        anchor.listen(64)
        monkeypatch.setattr(heal, "CONNECTIVITY_ANCHORS", (anchor.getsockname(),))
        yield anchor


def go_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heal, "CONNECTIVITY_ANCHORS", (("127.0.0.1", free_port()),))


def _init(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, machine: FakeMachine, *units: str) -> None:
    stand_in_for_the_network(monkeypatch)
    assert run_cli(capsys, *init_args()).code == 0
    for unit in units or (BRIDGE, SHIM):
        (machine.state_dir / f"{unit}.active").write_text("active")


def restarts(machine: FakeMachine) -> list[str]:
    """Heal's restarts, as `restart <unit>`; heal only ever runs `try-restart --job-mode=fail <unit>`."""
    prefix = "try-restart --job-mode=fail "
    assert not [call for call in machine.calls() if call.startswith("restart ")]
    return [f"restart {call.removeprefix(prefix)}" for call in machine.calls() if call.startswith(prefix)]


def heal_pass(capsys: pytest.CaptureFixture[str]) -> Result:
    return run_cli(capsys, "doctor", "--heal")


def test_a_stop_during_the_pass_wins_over_the_restart(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    internet: socket.socket,
) -> None:
    """`computer chatgpt end` finishing while heal probes must not have the stopped bridge started again."""
    _init(capsys, monkeypatch, machine)
    with hung_server(load_settings().proxy_port):
        for _ in range(2):
            heal_pass(capsys)
            clock.now += 240
        found = heal.watched

        def stopped_while_probing(ctx: Context) -> list[watchdog.Watched]:
            def stopping(probe: Callable[[], str | None]) -> Callable[[], str | None]:
                def run() -> str | None:
                    for unit in (BRIDGE, SHIM):
                        (machine.state_dir / f"{unit}.active").write_text("inactive")
                    return probe()

                return run

            return [replace(item, probe=stopping(item.probe)) for item in found(ctx)]

        monkeypatch.setattr(heal, "watched", stopped_while_probing)
        third = heal_pass(capsys)
    assert machine.unit_state(BRIDGE, "active") == "inactive"
    assert f"heal: {BRIDGE} stopped during the pass: not restarted" in third.out
    assert "restarted" not in third.out.replace("not restarted", "")


def test_a_hung_bridge_is_restarted_after_the_threshold_and_the_budget_stops_a_crash_loop(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    internet: socket.socket,
) -> None:
    _init(capsys, monkeypatch, machine)
    with hung_server(load_settings().proxy_port):
        first = heal_pass(capsys)
        clock.now += 240
        second = heal_pass(capsys)
        assert f"{BRIDGE} unhealthy (1/3)" in first.out and f"{BRIDGE} unhealthy (2/3)" in second.out
        assert restarts(machine) == []  # not before the threshold
        # The shim proxies the bridge, so it hangs too; that is the bridge's failure, not the shim's.
        assert f"{SHIM} fails while {BRIDGE} does not serve" in first.out

        clock.now += 240
        third = heal_pass(capsys)
        assert restarts(machine) == [f"restart {BRIDGE}"]
        assert f"restarted {BRIDGE} (1 of 3 this hour)" in third.out

        outcomes: list[Result] = []
        for _ in range(9):  # the bridge stays hung after every restart: a crash loop, one pass every 4 minutes
            clock.now += 240
            outcomes.append(heal_pass(capsys))
        assert restarts(machine) == [f"restart {BRIDGE}"] * 3
        spent = outcomes[-1]
        assert spent.code == 3
        assert "restart budget is spent (3 in the last hour): not restarting it" in spent.out

        for _ in range(6):  # until the first restart is an hour old: then one more is allowed
            clock.now += 240
            heal_pass(capsys)
        assert restarts(machine) == [f"restart {BRIDGE}"] * 4


def test_nothing_is_restarted_while_the_internet_is_down_and_one_line_says_so(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    internet: socket.socket,
) -> None:
    _init(capsys, monkeypatch, machine)
    go_offline(monkeypatch)
    with hung_server(load_settings().proxy_port):
        for _ in range(6):
            clock.now += 700
            result = heal_pass(capsys)
            assert result.code == 0
            gate = [line for line in result.out.splitlines() if "internet" in line]
            assert gate == ["heal: the internet is unreachable: no restarts while it is down (failure counts start again)"]
        assert restarts(machine) == []
        monkeypatch.setattr(heal, "CONNECTIVITY_ANCHORS", (internet.getsockname(),))
        # The failures counted during the outage do not carry over: the threshold starts again.
        assert "unhealthy (1/3)" in heal_pass(capsys).out
        assert restarts(machine) == []


def test_cloudflared_is_judged_by_its_own_ready_endpoint(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    internet: socket.socket,
) -> None:
    _init(capsys, monkeypatch, machine, BRIDGE, SHIM, TUNNEL)
    settings = replace(load_settings(), tunnel_name="test-box", metrics_port=free_port())
    save_settings(settings)
    with serving(settings.proxy_port), serving(settings.shim_port), serving(settings.metrics_port):
        outputs = [heal_pass(capsys).out for _ in range(3)]
    assert "/ready answered 503: no edge connection" in outputs[0]
    assert restarts(machine) == [f"restart {TUNNEL}"]  # bridge and shim answer, so they are left alone


def test_a_hung_shim_in_front_of_a_serving_bridge_is_the_one_restarted(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    internet: socket.socket,
) -> None:
    _init(capsys, monkeypatch, machine)
    settings = load_settings()
    with serving(settings.proxy_port), hung_server(settings.shim_port):
        for _ in range(3):
            heal_pass(capsys)
    assert restarts(machine) == [f"restart {SHIM}"]


def test_a_unit_that_is_not_active_is_left_to_systemd(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    internet: socket.socket,
) -> None:
    _init(capsys, monkeypatch, machine)
    (machine.state_dir / f"{BRIDGE}.active").write_text("failed")
    (machine.state_dir / f"{SHIM}.active").write_text("activating")
    with hung_server(load_settings().proxy_port):
        outputs = [heal_pass(capsys).out for _ in range(5)]
    assert restarts(machine) == []
    assert f"heal: {BRIDGE} is failed: systemd restarts it, heal does not" in outputs[-1]


def test_a_second_pass_never_overlaps_one_that_holds_the_lock(
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    internet: socket.socket,
) -> None:
    _init(capsys, monkeypatch, machine)
    lock = layout().state_dir / heal.LOCK_FILE
    with hung_server(load_settings().proxy_port):
        for _ in range(2):
            heal_pass(capsys)
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            blocked = heal_pass(capsys)
        assert blocked.code == 0 and "another heal pass holds" in blocked.out
        assert restarts(machine) == []  # the third failing pass did not run, so the threshold is not reached
        heal_pass(capsys)
    assert restarts(machine) == [f"restart {BRIDGE}"]


def test_heal_takes_no_json_or_local(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(capsys, monkeypatch, machine)
    for flag in ("--json", "--local"):
        result = run_cli(capsys, "doctor", "--heal", flag)
        assert result.code == 2 and "--heal takes no --json or --local" in result.err
    assert restarts(machine) == []


def test_priority_prefixes_only_on_the_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with (tmp_path / "stream").open("w") as stream:
        info = (tmp_path / "stream").stat()
        monkeypatch.setenv("JOURNAL_STREAM", f"{info.st_dev}:{info.st_ino}")
        assert watchdog.on_journal(stream)
        monkeypatch.setenv("JOURNAL_STREAM", "1:2")
        assert not watchdog.on_journal(stream)
        monkeypatch.delenv("JOURNAL_STREAM")
        assert not watchdog.on_journal(stream)


def test_heal_refuses_the_tunnel_mode(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    assert run_cli(capsys, *tunnel_init_args()).code == 0
    result = run_cli(capsys, "doctor", "--heal")
    assert result.code == 2 and "covers the public mode only" in result.err
    assert restarts(machine) == []


def test_warnings_only_when_the_pass_acts(tmp_path: Path) -> None:
    """The journal gets a warning for a restart and for the first pass the budget stops one; everything else is quieter."""
    said: list[tuple[int, str]] = []
    restarted: list[str] = []
    now = [0.0]
    policy = watchdog.Policy(threshold=2, budget=1, spacing=0.0)

    def restart(unit: str) -> bool:
        restarted.append(unit)
        return True

    def run(probe: watchdog.Watched) -> None:
        watchdog.heal_pass(
            [probe],
            state_file=tmp_path / "heal.json",
            active_state=lambda _unit: "active",
            restart=restart,
            online=lambda: True,
            clock=lambda: now[0],
            say=lambda priority, text: said.append((priority, text)),
            policy=policy,
        )
        now[0] += 60

    healthy = watchdog.Watched("a.service", lambda: None)
    hung = watchdog.Watched("a.service", lambda: "hung")
    run(healthy)
    for _ in range(6):  # fail, restart, fail, refused (warning), refused, refused
        run(hung)
    warnings = [text for priority, text in said if priority == watchdog.WARNING]
    assert restarted == ["a.service"]
    assert len(warnings) == 2
    assert warnings[0].startswith("heal: restarted a.service") and "budget is spent" in warnings[1]
    assert said[0] == (watchdog.DEBUG, "heal: 1 unit(s) healthy, nothing to do")
