"""The managed, pinned Desktop Commander install that every client adapter launches.

HOW IT IS REACHED: `computer mcp install` calls `install()`, `computer mcp serve` calls `launch_spec()`, and client adapters call
`install()` so their one command sets everything up.
INVARIANTS: one exact version from `pins.py` lives under `${XDG_DATA_HOME:-~/.local/share}/computer/desktop-commander`
(npm --prefix, never the floating npx cache); `install.json` records the absolute `node` path that was current at install
time so launching needs no PATH (a desktop app autostarts with a bare one); `install()` is idempotent and runs npm only when the
pinned version is absent.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from agenttools_errors import AgentToolError, MissingDepError, MissingTargetError, NetworkError

from computerlib.fileio import atomic_write
from computerlib.jsonvalue import as_dict, is_object
from computerlib.paths import data_home
from computerlib.pins import DESKTOP_COMMANDER_PACKAGE, DESKTOP_COMMANDER_VERSION

# What a healthy Desktop Commander lists; the handshake in `status` checks for these.
EXPECTED_TOOLS = ("start_process", "read_file", "write_file")

_NPM_TIMEOUT_SECONDS = 900
_MIN_NODE_MAJOR = 18
_NETWORK_CODES = frozenset(
    {
        "ENOTFOUND",
        "EAI_AGAIN",
        "ETIMEDOUT",
        "ECONNREFUSED",
        "ECONNRESET",
        "ENETUNREACH",
        "EHOSTUNREACH",
        "EAI_FAIL",
        "EPROTO",
        "ERR_SOCKET_TIMEOUT",
        "FETCH_ERROR",
        "CERT_HAS_EXPIRED",
        "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
    }
)


@dataclass(frozen=True)
class LaunchSpec:
    node: Path
    entry: Path


def install_dir() -> Path:
    return data_home() / "computer" / "desktop-commander"


def _record_path() -> Path:
    return install_dir() / "install.json"


def _entry_path() -> Path:
    return install_dir() / "node_modules" / DESKTOP_COMMANDER_PACKAGE / "dist" / "index.js"


def _read_record() -> dict[str, object] | None:
    try:
        data: object = json.loads(_record_path().read_text())
    except (OSError, ValueError):
        return None
    return as_dict(data) if is_object(data) else None


def _write_record(node: str) -> None:
    record = {
        "package": DESKTOP_COMMANDER_PACKAGE,
        "version": DESKTOP_COMMANDER_VERSION,
        "node": node,
    }
    atomic_write(_record_path(), json.dumps(record, indent=2) + "\n")


@contextmanager
def _exclusive_install_lock() -> Generator[None]:
    """Two installs (`computer mcp install` and an adapter's) must not run npm in one prefix at once."""
    install_dir().mkdir(parents=True, exist_ok=True)
    with open(install_dir() / ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _require(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise MissingDepError(
            what=f"{name} is not installed",
            why=f"Desktop Commander is an npm package; installing it needs {name} on PATH",
            fix="install Node.js (https://nodejs.org) and re-run `computer mcp install`",
            install="see https://nodejs.org (Node 18 or newer)",
        )
    return os.path.abspath(found)


def _real_node(found: str) -> str:
    """The node binary to record: stable where possible, never a version-manager shim, and at least `_MIN_NODE_MAJOR`."""
    expression = "process.execPath + '\\n' + process.versions.node"
    try:
        done = subprocess.run([found, "-p", expression], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return found
    reported, _, version = done.stdout.strip().partition("\n")
    if done.returncode != 0 or not os.path.isabs(reported) or not os.access(reported, os.X_OK):
        return found
    major = version.split(".")[0]
    if major.isdigit() and int(major) < _MIN_NODE_MAJOR:
        raise MissingDepError(
            what=f"node {version} is too old for Desktop Commander",
            why=f"{found} is Node {version}; Desktop Commander needs Node {_MIN_NODE_MAJOR} or newer",
            fix="install a newer Node.js (https://nodejs.org) ahead of it on PATH and re-run `computer mcp install`",
        )
    # A symlink that resolves to the reported binary (brew, nvm) keeps its stable name so an upgrade does not strand the record,
    # and so does snap's launcher symlink. Anything else resolves elsewhere: a shim (volta, mise) that needs its manager.
    stable = os.path.realpath(found) == os.path.realpath(reported) or found.startswith("/snap/")
    return found if stable else reported


def install() -> str:
    """Make the pinned install present and return a one-line report. Raises AgentToolError (127 no node/npm, 7 network)."""
    node = _real_node(_require("node"))
    with _exclusive_install_lock():
        record = _read_record()
        if record is not None and record.get("version") == DESKTOP_COMMANDER_VERSION and _entry_path().is_file():
            if record.get("node") == node:
                return f"desktop-commander {DESKTOP_COMMANDER_VERSION} already installed in {install_dir()}"
            _write_record(node)
            return f"desktop-commander {DESKTOP_COMMANDER_VERSION} already installed; recorded node {node}"
        _run_npm(_require("npm"), node)
        _write_record(node)
    return f"installed desktop-commander {DESKTOP_COMMANDER_VERSION} in {install_dir()}"


def _run_npm(npm: str, node: str) -> None:
    # npm's own shebang is `env node`, so the node we are about to record must be first on its PATH.
    env = {**os.environ, "PATH": f"{os.path.dirname(node)}{os.pathsep}{os.environ.get('PATH', '')}"}
    spec = f"{DESKTOP_COMMANDER_PACKAGE}@{DESKTOP_COMMANDER_VERSION}"
    command = [
        npm,
        "install",
        "--prefix",
        str(install_dir()),
        "--save-exact",
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
        spec,
    ]
    # Own session so a timeout can stop npm's whole process tree, not leave scripts running past the lock.
    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=_NPM_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        with suppress(ProcessLookupError):  # the tree may have exited between the timeout and here
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise NetworkError(
            what="npm install timed out",
            why=f"npm did not finish within {_NPM_TIMEOUT_SECONDS}s installing {spec}",
            fix="check the network and re-run `computer mcp install`",
        ) from exc
    if proc.returncode != 0:
        output = (stderr or stdout).strip()
        # Classify on npm's own `code E...` line (free text such as "check your network" also appears in permission errors),
        # over the full output, and show only the tail.
        code = re.search(r"\bcode ([A-Z][A-Z_]+)\b", output)
        error_class = NetworkError if code is not None and code.group(1) in _NETWORK_CODES else AgentToolError
        raise error_class(
            what=f"npm install of {spec} failed",
            why=output[-600:] or f"npm exited {proc.returncode}",
            fix="fix the cause above and re-run `computer mcp install`",
        )
    if not _entry_path().is_file():
        raise AgentToolError(
            what="npm reported success but the Desktop Commander entry file is missing",
            why=f"expected {_entry_path()}",
            fix="remove the managed directory and re-run `computer mcp install`",
        )


def launch_spec() -> LaunchSpec:
    """The recorded node and entry file, validated; raises MissingTargetError (exit 5) with the fix when anything is off."""
    fix = "run `computer mcp install`"
    record = _read_record()
    if record is None:
        raise MissingTargetError(what="Desktop Commander is not installed", why=f"no {_record_path()}", fix=fix)
    if record.get("version") != DESKTOP_COMMANDER_VERSION:
        raise MissingTargetError(
            what="the installed Desktop Commander is not the pinned version",
            why=f"installed {record.get('version')}, pinned {DESKTOP_COMMANDER_VERSION}",
            fix=fix,
        )
    node = Path(str(record.get("node", "")))
    if not (node.is_file() and os.access(node, os.X_OK)):
        raise MissingTargetError(what="the recorded node binary is gone", why=f"{node} is not an executable file", fix=fix)
    if not _entry_path().is_file():
        raise MissingTargetError(what="the Desktop Commander files are missing", why=f"no {_entry_path()}", fix=fix)
    return LaunchSpec(node=node, entry=_entry_path())
