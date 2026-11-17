"""Thin wrapper over `systemctl --user` for the units of this stack.

WHAT: write unit files, reload, enable/start/stop/restart/disable and read the state of a unit.
INVARIANTS: only units whose names the caller passes are touched (never a pattern); a unit file is rewritten, and the manager
reloaded, only when its text changed; enabling lingering is a separate, explicit call so the stack survives logout and starts at
boot.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agenttools_errors import AgentToolError, MissingDepError

# "unknown" is what `show` reports when the manager cannot be reached: that is not evidence the unit is off.
_LIVE_STATES = frozenset({"active", "activating", "deactivating", "reloading", "refreshing", "unknown"})

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class UnitState:
    name: str
    active: str  # active | inactive | failed | activating | not-found ...
    sub: str
    restarts: int
    enabled: bool


class Systemd:
    def __init__(self, unit_dir: Path, runner: Runner = _run) -> None:
        self._unit_dir = unit_dir
        self._runner = runner

    def _ctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if shutil.which("systemctl") is None:
            raise MissingDepError(
                what="systemctl is not installed",
                why="the stack runs as systemd user units",
                fix="use `computer chatgpt run` to run it in the foreground without systemd",
                install="sudo apt install systemd",
            )
        result = self._runner(["systemctl", "--user", *args])
        if check and result.returncode != 0:
            raise AgentToolError(
                what=f"systemctl --user {' '.join(args)} failed",
                why=(result.stderr or result.stdout).strip() or f"exit {result.returncode}",
                fix="check `systemctl --user status` and that a user session/manager is running (XDG_RUNTIME_DIR)",
            )
        return result

    def require_manager(self) -> None:
        """The user manager must answer before anything is written: units are useless without it."""
        result = self._ctl("show-environment", check=False)
        if result.returncode != 0:
            raise MissingDepError(
                what="no systemd user manager is reachable (`systemctl --user`)",
                why=(result.stderr or result.stdout).strip() or f"exit {result.returncode}",
                fix="run the command in a login session of this user (desktop or ssh, not sudo/su) so XDG_RUNTIME_DIR is set",
            )

    def install(self, units: dict[str, str]) -> list[str]:
        """Write the unit files; returns the names that changed. The manager is reloaded once when any did."""
        self._unit_dir.mkdir(parents=True, exist_ok=True)
        changed: list[str] = []
        for name, text in units.items():
            path = self._unit_dir / name
            if path.exists() and path.read_text() == text:
                continue
            temp = path.with_suffix(".tmp")
            temp.write_text(text)
            temp.replace(path)
            changed.append(name)
        if changed:
            self._ctl("daemon-reload")
        return changed

    def enable_now(self, names: list[str]) -> None:
        self._ctl("enable", "--now", *names)

    def start(self, names: list[str]) -> None:
        self._ctl("start", *names)

    def stop(self, names: list[str]) -> None:
        """Stop the units. A failing `systemctl stop` is an error unless every unit is verifiably not running afterwards
        (an unknown unit makes systemctl fail but is already stopped)."""
        result = self._ctl("stop", *names, check=False)
        if result.returncode != 0:
            self._require_not_running("stop", names, result)

    def _require_not_running(
        self, command: str, names: list[str], failed: subprocess.CompletedProcess[str], *, also_disabled: bool = False
    ) -> None:
        left: list[str] = []
        for name in names:
            state = self.state(name)
            if state.active in _LIVE_STATES or (also_disabled and state.enabled):
                left.append(f"{name} ({state.active}{', enabled' if state.enabled else ''})")
        if left:
            raise AgentToolError(
                what=f"systemctl --user {command} failed: still on: {', '.join(left)}",
                why=(failed.stderr or failed.stdout).strip() or f"exit {failed.returncode}",
                fix=(
                    "check `systemctl --user status` and that a user session is running (XDG_RUNTIME_DIR), "
                    "then run the command again"
                ),
            )

    def restart(self, names: list[str]) -> None:
        self._ctl("restart", *names)

    def disable_now(self, names: list[str]) -> None:
        """Stop and disable the units. Success is judged by the resulting state, never by the exit code alone: access must really
        be off, so a unit still running or still enabled afterwards is an error (an unknown unit is not)."""
        result = self._ctl("disable", "--now", *names, check=False)
        if result.returncode != 0:
            self._require_not_running("disable --now", names, result, also_disabled=True)

    def remove(self, names: list[str]) -> None:
        for name in names:
            (self._unit_dir / name).unlink(missing_ok=True)
        self._ctl("daemon-reload", check=False)
        # A unit that exited non-zero on stop stays listed as "not-found failed" after its file is gone; forget it too.
        self._ctl("reset-failed", *names, check=False)

    def state(self, name: str) -> UnitState:
        shown = self._ctl(
            "show", name, "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts", "-p", "UnitFileState", check=False
        )
        fields = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
        restarts = fields.get("NRestarts", "0")
        return UnitState(
            name=name,
            active=fields.get("ActiveState", "unknown"),
            sub=fields.get("SubState", ""),
            restarts=int(restarts) if restarts.isdigit() else 0,
            enabled=fields.get("UnitFileState", "") == "enabled",
        )

    def lingering(self, user: str) -> bool:
        result = self._runner(["loginctl", "show-user", user, "-p", "Linger", "--value"])
        return result.returncode == 0 and result.stdout.strip() == "yes"

    def enable_lingering(self, user: str) -> None:
        result = self._runner(["loginctl", "enable-linger", user])
        if result.returncode != 0:
            raise AgentToolError(
                what="cannot enable lingering",
                why=(result.stderr or result.stdout).strip() or f"exit {result.returncode}",
                fix=f"run `sudo loginctl enable-linger {user}` so the stack starts at boot without a login",
            )
