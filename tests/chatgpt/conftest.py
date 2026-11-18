"""Fakes for the machine boundary: `systemctl`, `loginctl`, `node` and `npm` as real executables on PATH, so commands run their
real code (the node/npm pair is the shared one of tests/fakes.py: the core Desktop Commander install runs for real against it)."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from computerlib.targets.chatgpt import machine as machine_identity
from tests.fakes import fake_node, fake_npm

SYSTEM_HOSTNAME = "Test_Box.lan"  # the machine name every test sees by default: `test-box`


@pytest.fixture(autouse=True)
def system_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine name must not depend on the host the tests run on."""
    monkeypatch.setattr(machine_identity, "system_hostname", lambda: SYSTEM_HOSTNAME)


SYSTEMCTL = '''#!/usr/bin/env python3
"""Fake `systemctl --user`: keeps per-unit state in $FAKE_SYSTEMD_DIR and logs every call."""
import os, sys
from pathlib import Path
state = Path(os.environ["FAKE_SYSTEMD_DIR"])
state.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
assert args[0] == "--user", args
args = args[1:]
with (state / "calls.log").open("a") as log:
    log.write(" ".join(args) + "\\n")
def units(rest):
    return [a for a in rest if a.endswith(".service")]
def put(unit, **kv):
    for k, v in kv.items():
        (state / f"{unit}.{k}").write_text(v)
def get(unit, k, default):
    p = state / f"{unit}.{k}"
    return p.read_text() if p.exists() else default
cmd = args[0]
if cmd in os.environ.get("FAKE_SYSTEMD_FAIL", "").split(","):
    sys.stderr.write("Failed to " + cmd + ": Access denied\\n")
    sys.exit(1)
if cmd in ("enable", "start", "restart"):
    for u in units(args):
        put(u, active="active")
        if cmd == "enable":
            put(u, enabled="enabled")
elif cmd in ("stop", "disable"):
    for u in units(args):
        put(u, active="inactive")
        if cmd == "disable":
            put(u, enabled="disabled")
elif cmd == "show":
    u = args[1]
    print("ActiveState=" + get(u, "active", "inactive"))
    print("SubState=" + ("running" if get(u, "active", "inactive") == "active" else "dead"))
    print("NRestarts=0")
    print("UnitFileState=" + get(u, "enabled", "disabled"))
'''

LOGINCTL = """#!/usr/bin/env python3
import os, sys
from pathlib import Path
state = Path(os.environ["FAKE_SYSTEMD_DIR"])
state.mkdir(parents=True, exist_ok=True)
flag = state / "linger"
if sys.argv[1] == "show-user":
    print("yes" if flag.exists() else "no")
elif sys.argv[1] == "enable-linger":
    flag.write_text("yes")
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@dataclass
class FakeMachine:
    bin_dir: Path
    state_dir: Path

    def calls(self) -> list[str]:
        log = self.state_dir / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def unit_state(self, unit: str, key: str) -> str:
        path = self.state_dir / f"{unit}.{key}"
        return path.read_text() if path.exists() else ""


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeMachine:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    state = tmp_path / "systemd-state"
    _executable(bin_dir / "systemctl", SYSTEMCTL)
    _executable(bin_dir / "loginctl", LOGINCTL)
    fake_node(bin_dir)
    fake_npm(bin_dir)
    monkeypatch.setenv("FAKE_SYSTEMD_DIR", str(state))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return FakeMachine(bin_dir, state)
