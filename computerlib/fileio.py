"""Atomic file replacement with an optional timestamped backup, shared by every command that edits a user's file.

INVARIANTS: the target is either fully the old content or fully the new content (temp file in the same directory, fsync,
rename); the file mode of an existing target is kept; a symlinked target (stow, chezmoi) is written THROUGH, the link stays;
a backup is a sibling `<name>.bak-<UTC timestamp>` created with the original's mode from the first byte (a 0600 config never
has a world-readable copy), made before the write.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def backup(path: Path) -> Path:
    real = path.resolve()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    copy = real.with_name(f"{real.name}.bak-{stamp}")
    fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, real.stat().st_mode & 0o777)
    with os.fdopen(fd, "wb") as handle:
        handle.write(real.read_bytes())
    return copy


def atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    real = path.resolve()
    real.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = real.stat().st_mode & 0o777 if real.exists() else 0o666 & ~_umask()
    fd, temp_name = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(mode)
        os.replace(temp, real)
        dir_fd = os.open(real.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
