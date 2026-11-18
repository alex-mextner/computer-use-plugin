"""The login password of the OAuth page (public mode): generated locally, kept in a 0600 env file, never printed or logged.

The file holds `PASSWORD=<value>` and is read by systemd (`EnvironmentFile=`) or by `run` into the proxy's environment, so the
value is never in an argv. The keychain CLI is the preferred store when the user wants one; this file is the default so the stack
starts at boot without an unlocked keychain.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from computerlib.targets.chatgpt.privatefile import write_private

PASSWORD_VARIABLE = "PASSWORD"


def ensure_password(path: Path) -> bool:
    """Create the env file with a new random password when it has none; returns whether it was created."""
    if read_password(path):
        path.chmod(0o600)
        return False
    write_private(path, f"{PASSWORD_VARIABLE}={secrets.token_urlsafe(24)}\n")
    return True


def read_password(path: Path) -> str:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        name, _, value = line.partition("=")
        if name.strip() == PASSWORD_VARIABLE:
            return value.strip().strip("'\"")
    return ""
