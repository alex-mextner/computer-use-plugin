from __future__ import annotations

from pathlib import Path

from computerlib.targets.chatgpt.accessview import Filter, LogTail, Record, parse_line, read_records, select

LINE = "2026-11-08T10:00:00.000Z 200 POST /mcp 3ms client=openai-mcp rpc=tools/call tool=get_config suppressed=4"


def test_a_line_parses_into_its_fields() -> None:
    record = parse_line(LINE)
    assert record is not None
    assert (record.status, record.method, record.path, record.client) == (200, "POST", "/mcp", "openai-mcp")
    assert record.rpc == ("tools/call",) and record.tool == ("get_config",)
    assert record.verified_openai


def test_garbage_and_secrets_looking_lines_do_not_parse() -> None:
    assert parse_line("not a log line") is None
    assert parse_line("") is None


def test_rotated_files_are_read_oldest_first_only_when_asked(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    (tmp_path / "access.log.2").write_text("2026-11-08T08:00:00.000Z 200 GET /a 1ms client=other\n")
    (tmp_path / "access.log.1").write_text("2026-11-08T09:00:00.000Z 200 GET /b 1ms client=other\n")
    log.write_text("2026-11-08T10:00:00.000Z 200 GET /c 1ms client=other\n")
    assert [r.path for r in read_records(log, include_rotated=True)] == ["/a", "/b", "/c"]
    assert [r.path for r in read_records(log)] == ["/c"]


def test_filters_combine(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    log.write_text(LINE + "\n2026-11-08T10:00:01.000Z 401 POST /.auth/login 3ms client=unverified\n")
    records = read_records(log)
    assert len(select(records, Filter(oauth=True), 10)) == 1
    assert len(select(records, Filter(status=200, client="openai-mcp", rpc="tools/call"), 10)) == 1
    assert select(records, Filter(tool="other"), 10) == []


def _line(path: str) -> str:
    return f"2026-11-08T10:00:09.000Z 200 GET {path} 1ms client=other\n"


def _paths(records: list[Record]) -> list[str]:
    return [r.path for r in records]


def test_a_tail_returns_only_new_complete_lines(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    log.write_text(LINE + "\n")
    tail = LogTail(log)
    with log.open("a") as handle:
        handle.write(_line("/x") + "2026-11-08T10:00:03.000Z 200 GET /half")
    assert _paths(tail.poll()) == ["/x"]
    with log.open("a") as handle:
        handle.write(" 1ms client=other\n")
    assert _paths(tail.poll()) == ["/half"]
    assert tail.poll() == []
    tail.close()


def test_a_tail_of_a_log_that_appears_later_reads_it_from_the_start(tmp_path: Path) -> None:
    log = tmp_path / "state" / "access.log"
    tail = LogTail(log)
    assert tail.poll() == []
    log.parent.mkdir()
    log.write_text(_line("/first") + _line("/second"))
    assert _paths(tail.poll()) == ["/first", "/second"]
    tail.close()


def test_a_log_cut_down_in_place_is_read_from_its_start(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    log.write_text(_line("/a") + _line("/b"))
    tail = LogTail(log)
    log.write_text(_line("/fresh"))
    assert _paths(tail.poll()) == ["/fresh"]
    tail.close()


def _rotate(log: Path, keep: int = 3) -> None:
    """What the shim's writer does: file.N-1 -> file.N, active -> file.1, then a new active file."""
    for n in range(keep - 1, 0, -1):
        source = Path(f"{log}.{n}")
        if source.exists():
            source.rename(f"{log}.{n + 1}")
    log.rename(f"{log}.1")


def test_a_rotation_does_not_lose_what_the_old_file_still_held_unread(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    log.write_text(_line("/seen"))
    tail = LogTail(log)
    with log.open("a") as handle:
        handle.write(_line("/old-unread-1") + _line("/old-unread-2"))
    _rotate(log)
    log.write_text(
        _line("/new-1") + _line("/new-2") + _line("/new-3") + _line("/new-4")
    )  # longer than the old file was: size alone sees no shrink
    assert _paths(tail.poll()) == ["/old-unread-1", "/old-unread-2", "/new-1", "/new-2", "/new-3", "/new-4"]
    with log.open("a") as handle:
        handle.write(_line("/after"))
    assert _paths(tail.poll()) == ["/after"]
    tail.close()


def test_several_rotations_between_two_polls_lose_nothing_either(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    log.write_text("")
    tail = LogTail(log)
    with log.open("a") as handle:
        handle.write(_line("/a"))
    _rotate(log)
    log.write_text(_line("/b"))
    _rotate(log)
    log.write_text(_line("/c"))
    assert _paths(tail.poll()) == ["/a", "/b", "/c"]
    tail.close()


def test_a_tail_survives_the_moment_between_the_rename_and_the_new_file(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    log.write_text("")
    tail = LogTail(log)
    with log.open("a") as handle:
        handle.write(_line("/a"))
    _rotate(log)  # no active file yet
    assert _paths(tail.poll()) == ["/a"]
    log.write_text(_line("/b"))
    assert _paths(tail.poll()) == ["/b"]
    tail.close()
