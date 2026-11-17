"""The shim's compact access log: line format, noise collapsing and a size-capped rotating writer.

Line format, space separated, one request per line:
  <ISO time> <status> <method> <path without query> <latency>ms client=<label> [rpc=<method>[,..]] [tool=<name>[,..]]
    [suppressed=<n>]
NEVER written: query strings (OAuth codes and state live there), request or response bodies, tool arguments, Authorization
headers, cookies, passwords, tokens, the full User-Agent. The file is mode 0600 in a 0700 directory.
A log failure (disk full, EACCES) never touches request handling: lines are dropped and counted, one warning goes to stderr and a
recovery is reported once.
NOTE: the rotating writer is a generic utility (agent-tools agenttools_log is getting one); this copy is the minimum until it
ships.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

COLLAPSE_SECONDS = 60.0
COLLAPSE_KEYS_MAX = 256


@dataclass(frozen=True)
class Entry:
    time: str
    status: int
    method: str
    path: str
    millis: int
    client: str
    rpc: str = ""
    tool: str = ""
    suppressed: int = 0

    def render(self) -> str:
        parts = [self.time, str(self.status), self.method, self.path, f"{self.millis}ms", f"client={self.client}"]
        if self.rpc:
            parts.append(f"rpc={self.rpc}")
        if self.tool:
            parts.append(f"tool={self.tool}")
        if self.suppressed > 0:
            parts.append(f"suppressed={self.suppressed}")
        return " ".join(parts)


def safe_path(path: str) -> str:
    """The path as one printable token: no control or non-ASCII bytes, at most 120 characters."""
    return "".join(c if "!" <= c <= "~" else "?" for c in path[:120])


class Collapser:
    """Noise collapsing: the first request of a key is logged, the rest within a minute are only counted."""

    def __init__(self, window: float = COLLAPSE_SECONDS, keys_max: int = COLLAPSE_KEYS_MAX) -> None:
        self._window = window
        self._keys_max = keys_max
        self._seen: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str, now: float) -> int:
        """-1: collapsed away (do not log). >= 0: log it, with that many suppressed since the last logged one."""
        with self._lock:
            entry = self._seen.get(key)
            if entry is None:
                if len(self._seen) >= self._keys_max:
                    self._seen.popitem(last=False)
                self._seen[key] = (now, 0)
                return 0
            last, suppressed = entry
            if now - last < self._window:
                self._seen[key] = (last, suppressed + 1)
                return -1
            self._seen[key] = (now, 0)
            return suppressed


class RotatingLog:
    """Append-only, size-capped (file -> file.1 -> .. -> file.<keep>), failure-tolerant line writer."""

    def __init__(self, path: Path, max_bytes: int, keep: int) -> None:
        self.path = path
        self._max_bytes = max_bytes
        self._keep = keep
        self._fd: int | None = None
        self._size = 0
        self._lock = threading.Lock()
        self.dropped = 0
        self._warned = False

    def write(self, line: str) -> None:
        data = (line + "\n").encode()
        with self._lock:
            try:
                self._append(data)
            except OSError as error:
                self._fail(error)
                return
            if self._warned:
                self._warned = False
                print(
                    f"computer shim: access log is writable again ({self.dropped} lines were dropped meanwhile)", file=sys.stderr
                )
                self.dropped = 0

    def close(self) -> None:
        with self._lock:
            self._close_fd()

    def _fail(self, error: OSError) -> None:
        self.dropped += 1
        self._close_fd()
        if not self._warned:
            self._warned = True
            print(
                f"computer shim: cannot write the access log {self.path} ({error.strerror or error}); "
                "lines are dropped until it works again",
                file=sys.stderr,
            )

    def _close_fd(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = None

    def _open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self._size = os.fstat(fd).st_size
        return fd

    def _append(self, data: bytes) -> None:
        if self._fd is None:
            self._fd = self._open()
        if self._size > 0 and self._size + len(data) > self._max_bytes:
            self._rotate()
        os.write(self._fd, data)
        self._size += len(data)

    def _rotate(self) -> None:
        self._close_fd()
        if self._keep == 0:
            self.path.unlink(missing_ok=True)
        else:
            Path(f"{self.path}.{self._keep}").unlink(missing_ok=True)
            for n in range(self._keep - 1, 0, -1):
                source = Path(f"{self.path}.{n}")
                if source.exists():
                    source.rename(f"{self.path}.{n + 1}")
            self.path.rename(f"{self.path}.1")
        self._fd = self._open()


def now_iso() -> str:
    seconds = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{int(seconds * 1000) % 1000:03d}Z"
