"""Write a secret to a file that is private from the first byte, whatever the file looked like before.

WHY: the mode argument of `os.open` applies only to a NEW file, so writing a secret with `O_CREAT | O_TRUNC, 0o600` over an
existing 0644 file would leave it world-readable. The permissions are fixed on the open descriptor BEFORE the secret is written,
and the old content is truncated only after that. Used by the login password file and the tunnel-mode runtime key file.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_private(path: Path, text: str) -> None:
    """Replace the content of `path` with `text`, mode 0600, creating a missing directory as 0700."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
