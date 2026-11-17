"""Reading the shim's access log: parsing lines, filters, and the rotated files.

The line format is defined in shim/accesslog.py; this module is its reader (`computer chatgpt logs`, `status`, `connect`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

LINE = re.compile(
    r"^(?P<time>\S+) (?P<status>\d{3}) (?P<method>\S+) (?P<path>\S+) (?P<millis>\d+)ms client=(?P<client>\S+)"
    r"(?: rpc=(?P<rpc>\S+))?(?: tool=(?P<tool>\S+))?(?: suppressed=(?P<suppressed>\d+))?$"
)


@dataclass(frozen=True)
class Record:
    time: str
    status: int
    method: str
    path: str
    millis: int
    client: str
    rpc: tuple[str, ...]
    tool: tuple[str, ...]
    raw: str

    @property
    def is_oauth_step(self) -> bool:
        return self.path.startswith(("/.idp/", "/.auth/"))

    @property
    def verified_openai(self) -> bool:
        return self.client.startswith(("openai", "chatgpt"))


def parse_line(line: str) -> Record | None:
    match = LINE.match(line.strip())
    if match is None:
        return None
    return Record(
        time=match["time"],
        status=int(match["status"]),
        method=match["method"],
        path=match["path"],
        millis=int(match["millis"]),
        client=match["client"],
        rpc=tuple(match["rpc"].split(",")) if match["rpc"] else (),
        tool=tuple(match["tool"].split(",")) if match["tool"] else (),
        raw=line.strip(),
    )


def log_files(path: Path, include_rotated: bool) -> list[Path]:
    """The files to read, oldest first: rotated ones (file.N .. file.1) when asked, then the active file."""
    files: list[Path] = []
    if include_rotated:
        rotated = sorted(
            (p for p in path.parent.glob(f"{path.name}.*") if p.name.removeprefix(f"{path.name}.").isdigit()),
            key=lambda p: int(p.name.rsplit(".", 1)[1]),
            reverse=True,
        )
        files += rotated
    if path.exists():
        files.append(path)
    return files


def read_records(path: Path, include_rotated: bool = False) -> list[Record]:
    records: list[Record] = []
    for file in log_files(path, include_rotated):
        try:
            text = file.read_text(errors="replace")
        except OSError:
            continue
        records += [r for r in map(parse_line, text.splitlines()) if r is not None]
    return records


@dataclass(frozen=True)
class Filter:
    tool: str | None = None
    rpc: str | None = None
    status: int | None = None
    client: str | None = None
    oauth: bool = False

    def matches(self, record: Record) -> bool:
        checks = (
            self.tool is None or self.tool in record.tool,
            self.rpc is None or self.rpc in record.rpc,
            self.status is None or self.status == record.status,
            self.client is None or self.client == record.client,
            not self.oauth or record.is_oauth_step,
        )
        return all(checks)


def select(records: Iterable[Record], wanted: Filter, last: int) -> list[Record]:
    return [r for r in records if wanted.matches(r)][-last:] if last > 0 else []


class LogTail:
    """Follows the active access log like `tail -F`: new complete lines only, across rotation.

    It holds the file it reads open, so a rotation (the writer renames the active file to `.1` and starts a new one) cannot take
    unread lines away: when the path starts naming a different file, the old one is drained to its end first, then any files
    rotated in between (found by the old file's identity among `.N`), then the new active file from its start. A file cut down in
    place is read from its start. A line half written at the moment of a rotation is dropped (the writer appends whole lines).
    PAST BUG: tracking only the size, a replaced file longer than the offset was read from the middle and the unread lines of the
    old one were lost, so `connect` could miss the very requests it waits for.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None
        self._partial = b""
        self._open(at_end=True)

    def _open(self, *, at_end: bool) -> None:
        try:
            handle = self._path.open("rb")
        except OSError:
            return
        if at_end:
            handle.seek(0, os.SEEK_END)
        self._handle = handle

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        self._partial = b""

    def _available(self) -> list[Record]:
        handle = self._handle
        if handle is None:
            return []
        if os.fstat(handle.fileno()).st_size < handle.tell():
            handle.seek(0)
            self._partial = b""
        data = self._partial + handle.read()
        complete = data[: data.rfind(b"\n") + 1]
        self._partial = data[len(complete) :]
        return _parse(complete)

    def _replaced(self) -> bool:
        if self._handle is None:
            return False
        try:
            current = self._path.stat()
        except OSError:
            return False  # between the rename and the new file: the next poll sees it
        held = os.fstat(self._handle.fileno())
        return (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)

    def _rotated_after(self, held: os.stat_result) -> list[Path]:
        """Rotated files newer than the one held, oldest first (several rotations can happen between two polls)."""
        rotated = [f for f in log_files(self._path, True) if f != self._path]
        for index, file in enumerate(rotated):
            try:
                seen = file.stat()
            except OSError:
                continue
            if (seen.st_dev, seen.st_ino) == (held.st_dev, held.st_ino):
                return rotated[index + 1 :]
        return []

    def poll(self) -> list[Record]:
        """The records appended since the last poll (or since the tail was opened)."""
        if self._handle is None:
            self._open(at_end=False)  # the log did not exist yet: everything in it is new
            return self._available()
        records = self._available()
        if self._replaced():
            handle = self._handle
            records += self._available()  # drain: the writer closed the old file before the new one appeared
            newer = self._rotated_after(os.fstat(handle.fileno()))
            self.close()
            for file in newer:
                try:
                    records += _parse(file.read_bytes())
                except OSError:
                    continue
            self._open(at_end=False)
            records += self._available()
        return records


def _parse(data: bytes) -> list[Record]:
    return [r for r in map(parse_line, data.decode("utf-8", "replace").splitlines()) if r is not None]
