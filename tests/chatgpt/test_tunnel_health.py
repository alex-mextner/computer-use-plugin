"""The tunnel-client state machine against a stand-in daemon serving the endpoint shapes of tunnel-client 0.0.17."""

from __future__ import annotations

from computerlib.targets.chatgpt.tunnel_health import Counts, parse_counts, read_state
from tests.chatgpt.fake_daemon import METRIC, Daemon, running_daemon


def test_nothing_listening_is_unavailable() -> None:
    assert read_state(1).state == "unavailable"


def test_a_daemon_that_is_not_ready_is_not_ready() -> None:
    with running_daemon(Daemon(ready=503)) as port:
        assert read_state(port).state == "not_ready"


def test_a_ready_daemon_whose_control_plane_poll_fails_awaits_the_poll() -> None:
    with running_daemon(Daemon(polling=False, failures=3)) as port:
        health = read_state(port)
    assert health.state == "awaiting_poll"
    assert "key" in health.detail


def test_a_polling_daemon_with_no_traffic_awaits_discovery() -> None:
    with running_daemon(Daemon()) as port:
        assert read_state(port).state == "awaiting_discovery"


def test_an_initialize_through_the_tunnel_is_discovery_and_not_yet_a_connection() -> None:
    with running_daemon(Daemon(counts={"initialize": 1, "tools/list": 1})) as port:
        assert read_state(port).state == "awaiting_tool_call"


def test_only_a_tool_call_through_the_tunnel_means_connected() -> None:
    with running_daemon(Daemon(counts={"initialize": 1, "tools/list": 1, "tools/call": 2})) as port:
        health = read_state(port)
    assert health.state == "connected"
    assert health.counts.tools_call == 2


def test_traffic_from_before_the_watch_started_does_not_count() -> None:
    daemon = Daemon(counts={"initialize": 4, "tools/list": 4, "tools/call": 9})
    with running_daemon(daemon) as port:
        baseline = Counts(4, 4, 9)
        assert read_state(port, baseline).state == "awaiting_discovery"
        daemon.counts["tools/list"] = 5
        assert read_state(port, baseline).state == "awaiting_tool_call"
        daemon.counts["tools/call"] = 10
        assert read_state(port, baseline).state == "connected"


def test_counters_of_the_same_method_on_several_label_sets_are_summed() -> None:
    text = (
        METRIC % ("tools/call", 2)
        + "\n"
        + METRIC.replace("main", "other") % ("tools/call", 3)
        + "\n"
        + METRIC % ("ping", 7)
        + "\n"
    )
    assert parse_counts(text) == Counts(0, 0, 5)
