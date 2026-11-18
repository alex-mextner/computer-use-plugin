"""Pinned release downloads of the chatgpt target: binaries (sha256) and one member of a pinned zip.

WHAT: `ensure_binary` downloads a pinned asset, verifies its sha256 and moves it into place atomically; `ensure_zip_member` does
the same for one file inside a pinned zip (digest of the zip checked before it is opened, digest of the member before it is
installed, nothing else is ever extracted). Desktop Commander is not installed here: it comes from the shared core
(`computer mcp install`).
INVARIANTS: idempotent (a file with the pinned digest is left alone), an installed file is replaced only after the download
verified, a failed or mismatching download leaves the installed file untouched, nothing here runs a downloaded file.
NOTE: a generic pinned-installer belongs in agent-tools/lib once one ships; this is the target-specific minimum until then.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agenttools_errors import AgentToolError, NetworkError

from computerlib.pins import BinaryPin, ZipMemberPin

Fetch = Callable[[str], bytes]


def urllib_fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise NetworkError(
            what=f"cannot download {url}",
            why=str(error),
            fix="check the network and re-run the command (the download is verified and retried from scratch)",
        ) from error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Installed:
    path: Path
    changed: bool


def _refuse(subject: str, expected: str, actual: str, url: str) -> AgentToolError:
    return AgentToolError(
        what=f"refusing {subject}: sha256 mismatch",
        why=f"expected {expected}, got {actual} from {url}",
        fix="nothing was installed; if the release was re-published, verify it and bump the pin table",
    )


def _place(payload: bytes, target: Path) -> None:
    """Write `payload` as an executable at `target` through a temp file in the same directory and an atomic rename."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(handle, "wb") as temp:
            temp.write(payload)
        os.chmod(temp_name, 0o755)
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def ensure_binary(pin: BinaryPin, bin_dir: Path, fetch: Fetch | None = None) -> Installed:
    fetch = fetch or urllib_fetch  # resolved at call time so a test can stand in for the network
    target = bin_dir / pin.name
    if target.is_file() and sha256_file(target) == pin.sha256:
        return Installed(target, changed=False)
    payload = fetch(pin.url)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != pin.sha256:
        raise _refuse(f"{pin.name} {pin.version}", pin.sha256, actual, pin.url)
    _place(payload, target)
    return Installed(target, changed=True)


def ensure_zip_member(pin: ZipMemberPin, bin_dir: Path, fetch: Fetch | None = None) -> Installed:
    """Install `pin.member` of the pinned zip as `bin_dir/<pin.name>`.

    The member is read into memory by its exact name (never extracted to a path the archive names, so a `../x` or absolute member
    name cannot write anywhere), exactly one entry must carry that name, and the other members (bundled cloudflared, licences) are
    ignored.
    """
    fetch = fetch or urllib_fetch
    target = bin_dir / pin.name
    if target.is_file() and sha256_file(target) == pin.member_sha256:
        return Installed(target, changed=False)
    archive = fetch(pin.url)
    actual = hashlib.sha256(archive).hexdigest()
    if actual != pin.zip_sha256:
        raise _refuse(f"{pin.name} {pin.version} (the zip)", pin.zip_sha256, actual, pin.url)
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as opened:
            matches = [info for info in opened.infolist() if info.filename == pin.member]
            if len(matches) != 1:
                raise AgentToolError(
                    what=f"refusing {pin.name} {pin.version}: the zip has {len(matches)} entries named {pin.member!r}",
                    why="the pinned zip holds exactly one such member",
                    fix="nothing was installed; verify the release and bump the pin table",
                )
            payload = opened.read(matches[0])
    except zipfile.BadZipFile as error:
        raise AgentToolError(
            what=f"refusing {pin.name} {pin.version}: the download is not a readable zip",
            why=str(error),
            fix="nothing was installed; re-run `computer chatgpt init`",
        ) from error
    member_actual = hashlib.sha256(payload).hexdigest()
    if member_actual != pin.member_sha256:
        raise _refuse(f"{pin.name} {pin.version} (member {pin.member!r})", pin.member_sha256, member_actual, pin.url)
    _place(payload, target)
    return Installed(target, changed=True)
