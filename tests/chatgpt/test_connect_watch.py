from __future__ import annotations

from pathlib import Path

from computerlib.targets.chatgpt.checklist import Checklist
from computerlib.targets.chatgpt.commands.connect import report_timeout, watch
from computerlib.targets.chatgpt.context import load_context
from computerlib.targets.chatgpt.settings import Settings, save_settings
from computerlib.targets.chatgpt.shim.accesslog import RotatingLog

T = "2026-11-08T10:00:00.000Z"


class Clock:
    def __init__(self, on_sleep: list[str], log: Path) -> None:
        self.now = 0.0
        self._pending = on_sleep
        self._log = log

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self._pending:
            with self._log.open("a") as handle:
                handle.write(self._pending.pop(0) + "\n")

    def clock(self) -> float:
        return self.now


def test_the_watch_reports_steps_as_they_arrive_and_stops_when_connected() -> None:
    save_settings(Settings(hostname="h.example.test"))
    ctx = load_context()
    log = ctx.layout.access_log
    log.parent.mkdir(parents=True)
    log.write_text(
        f"{T} 200 POST /mcp 1ms client=openai-mcp rpc=tools/call tool=old\n"
    )  # before connect started: must be ignored
    clock = Clock(
        [
            f"{T} 201 POST /.idp/register 1ms client=unverified",
            f"{T} 200 POST /mcp 1ms client=openai-mcp rpc=initialize",
            f"{T} 200 POST /mcp 9ms client=openai-mcp rpc=tools/call tool=get_config",
        ],
        log,
    )
    lines: list[str] = []
    checklist = watch(ctx, 600, out=lines.append, sleep=clock.sleep, clock=clock.clock)
    assert checklist.connected
    text = "\n".join(lines)
    assert "registered itself" in text and "opened an MCP session" in text
    assert clock.now < 10


def test_the_watch_gives_up_at_the_timeout_and_the_report_names_what_is_missing() -> None:
    save_settings(Settings(hostname="h.example.test"))
    ctx = load_context()
    clock = Clock([], ctx.layout.access_log)
    checklist = watch(ctx, 5, out=lambda line: None, sleep=clock.sleep, clock=clock.clock)
    assert not checklist.connected
    lines: list[str] = []
    report_timeout(checklist, out=lines.append)
    assert any("No request reached this PC" in line for line in lines)
    assert any("ran a tool" in line for line in lines)


def test_a_partial_progress_report_does_not_claim_silence() -> None:
    checklist = Checklist()
    checklist.done["discovery"] = "x"
    lines: list[str] = []
    report_timeout(checklist, out=lines.append)
    assert not any("No request reached" in line for line in lines)


def test_a_log_rotation_during_the_wait_loses_none_of_the_steps(tmp_path: Path) -> None:
    """The shim's real rotating writer rolls the log over while `connect` waits: the lines the old file held unread still
    count."""
    save_settings(Settings(hostname="h.example.test"))
    ctx = load_context()
    log = ctx.layout.access_log
    log.parent.mkdir(parents=True)
    log.write_text("")
    writer = RotatingLog(log, max_bytes=400, keep=3)
    pad = f"{T} 200 GET /pad 1ms client=other"

    class Rotating(Clock):
        def sleep(self, seconds: float) -> None:
            self.now += seconds
            if self.round == 0:
                writer.write(f"{T} 201 POST /.idp/register 1ms client=unverified")
                writer.write(f"{T} 200 POST /mcp 1ms client=openai-mcp rpc=initialize")
                while not Path(f"{log}.1").exists():  # the writer rotates once the file would pass its cap
                    writer.write(pad)
                writer.write(f"{T} 200 POST /mcp 1ms client=openai-mcp rpc=tools/list")
            else:
                writer.write(f"{T} 200 POST /mcp 9ms client=openai-mcp rpc=tools/call tool=get_config")
            self.round += 1

        round = 0

    clock = Rotating([], log)
    lines: list[str] = []
    checklist = watch(ctx, 600, out=lines.append, sleep=clock.sleep, clock=clock.clock)
    writer.close()
    text = "\n".join(lines)
    assert checklist.connected
    assert "registered itself" in text and "opened an MCP session" in text
