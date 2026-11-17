"""The managed, pinned Desktop Commander install that every client adapter launches.

HOW IT IS REACHED: `computer mcp install` calls `install()`, `computer mcp serve` calls `launch_spec()`, and client adapters call
`install()` so their one command sets everything up.
INVARIANTS: one exact version from `pins.py` lives under `${XDG_DATA_HOME:-~/.local/share}/computer/desktop-commander`
(npm --prefix, never the floating npx cache), installed with `npm ci --ignore-scripts` from the package-lock.json bundled in
`computerlib/data/desktop-commander`, so every transitive package is the locked version with its integrity hash and a broken or
re-published tarball elsewhere on npm cannot change or break the install; `install.json` records the absolute `node` path that was
current at install time so launching needs no PATH (a desktop app autostarts with a bare one) and the sha256 of the lock it was
installed from; `install()` is idempotent and runs npm only when the pinned version or that lock is absent.
Each lock gets its own tree, `trees/<lock sha256>`, built by npm ci in a staging directory and switched to by rewriting
`install.json`: npm ci deletes node_modules before downloading, so it never runs in the tree `serve` launches, and a failed
install leaves the working one in place. Older trees (and the node_modules of a pre-lock install) are never deleted by this
code: a client started from one may still be running and loading files from it; remove them by hand once clients restarted.
Install scripts stay off: Desktop Commander's postinstall only sends an installation ping and re-checks ripgrep (which comes
from a platform package, no script needed), puppeteer would download a browser nothing uses, fsevents is macOS only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import resources
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


def _trees_dir() -> Path:
    return install_dir() / "trees"


def _tree_root(record: dict[str, object] | None) -> Path | None:
    """Where the recorded install lives: `trees/<name>` as recorded, or the prefix itself for an install made before the lock
    (0.2.0 ran `npm install --prefix` there and recorded no lock)."""
    if record is None:
        return None
    if record.get("lock_sha256") is None:
        return install_dir()
    tree = record.get("tree")
    # The name is the lock digest, plus a generation suffix for a repair; nothing else may steer serve outside `trees/`.
    return _trees_dir() / tree if isinstance(tree, str) and re.fullmatch(r"[0-9a-f]{64}(-[0-9]+)?", tree) else None


def _entry_path(root: Path) -> Path:
    return root / "node_modules" / DESKTOP_COMMANDER_PACKAGE / "dist" / "index.js"


def _read_record() -> dict[str, object] | None:
    try:
        data: object = json.loads(_record_path().read_text())
    except (OSError, ValueError):
        return None
    return as_dict(data) if is_object(data) else None


def bundled_lock_files() -> tuple[bytes, bytes]:
    """(package.json, package-lock.json) shipped in this package: the whole Desktop Commander tree, pinned."""
    base = resources.files("computerlib").joinpath("data", "desktop-commander")
    return base.joinpath("package.json").read_bytes(), base.joinpath("package-lock.json").read_bytes()


def _write_record(node: str, lock_sha256: str, tree: Path) -> None:
    record = {
        "package": DESKTOP_COMMANDER_PACKAGE,
        "version": DESKTOP_COMMANDER_VERSION,
        "lock_sha256": lock_sha256,
        "tree": tree.name,
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
    platform = _node_platform(node)
    package_json, lock = bundled_lock_files()
    lock_sha256 = hashlib.sha256(lock).hexdigest()
    with _exclusive_install_lock():
        record = _read_record()
        active = _tree_root(record)
        if (
            record is not None
            and record.get("version") == DESKTOP_COMMANDER_VERSION
            and record.get("lock_sha256") == lock_sha256
            and active is not None
            and _is_complete(active, platform)
        ):
            if record.get("node") == node:
                return f"desktop-commander {DESKTOP_COMMANDER_VERSION} already installed in {install_dir()}"
            _write_record(node, lock_sha256, active)
            return f"desktop-commander {DESKTOP_COMMANDER_VERSION} already installed; recorded node {node}"
        tree = _trees_dir() / lock_sha256
        if tree.exists() and not _is_complete(tree, platform):
            # A damaged tree a client may still run from: build the repair as a new generation and leave this path alone.
            tree = _trees_dir() / f"{lock_sha256}-{time.time_ns()}"
        if not _is_complete(tree, platform):  # a complete tree of this lock (only the record was lost) is reused as it is
            _install_tree(_require("npm"), node, platform, package_json, lock, tree)
        # Also for a reused tree: an install interrupted after the tree was promoted never got to the browser download.
        browser_warning = _install_browser(node, tree)
        # Writing the record is the switch: until here `serve` keeps launching the previous, complete tree. Nothing older is
        # deleted or moved: a client started from it may run for days (this tool restarts none), and Desktop Commander keeps
        # loading files and its ripgrep binary from its tree.
        _write_record(node, lock_sha256, tree)
    report = f"installed desktop-commander {DESKTOP_COMMANDER_VERSION} in {install_dir()}"
    return f"{report}\nwarning: {browser_warning}" if browser_warning else report


def _install_browser(node: str, tree: Path) -> str | None:
    """Run only puppeteer's own browser download (its install script, which `--ignore-scripts` skipped); a warning on failure.

    Desktop Commander's write_pdf launches puppeteer without a browser path when its own lookup and download find nothing, and
    puppeteer then uses the browser this puts in its default cache (~/.cache/puppeteer), as the pre-lock `npm install` did.
    The shell and file tools do not need it, so a failure only warns."""
    script = tree / "node_modules" / "puppeteer" / "install.mjs"
    # From the tree: run from the caller's directory, puppeteer would load a `.puppeteerrc.cjs` found there as code.
    fix = f"re-run `cd {shlex.quote(str(tree))} && {shlex.quote(node)} {shlex.quote(str(script))}` once the network is back"
    fix += " (only PDF output needs it)"
    try:
        done = subprocess.run(
            [node, str(script)], cwd=tree, capture_output=True, text=True, timeout=_NPM_TIMEOUT_SECONDS, check=False
        )
        # The script prints a failed download and still exits 0, so ask puppeteer whether its browser is really there.
        check = subprocess.run(
            [node, "-e", _BROWSER_PRESENT_JS, str(tree / "node_modules" / "puppeteer")],
            # The tree, not the caller's directory: puppeteer loads a `.puppeteerrc.cjs` from the working directory as code.
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"the browser for PDF output was not downloaded ({error}); {fix}"
    if done.returncode != 0 or check.returncode != 0:
        detail = (done.stderr or done.stdout or check.stderr).strip()[-300:] or f"exit {done.returncode}"
        return f"the browser for PDF output was not downloaded ({detail}); {fix}"
    return None


# Exit 0 only when the browser puppeteer launches by default (no executablePath) exists; executablePath() is async in v25.
_BROWSER_PRESENT_JS = (
    "Promise.resolve(require(process.argv[1]).executablePath())"
    ".then((path) => process.exit(require('fs').existsSync(path) ? 0 : 1), () => process.exit(1))"
)


def _node_platform(node: str) -> tuple[str, str]:
    """The (os, cpu) the recorded node runs as: @vscode/ripgrep loads the package of exactly this platform."""
    expression = "process.platform + ' ' + process.arch"
    try:
        done = subprocess.run([node, "-p", expression], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        done = None
    if done is None or done.returncode != 0 or " " not in done.stdout.strip():
        raise AgentToolError(
            what=f"cannot tell which platform {node} runs on",
            why=f"`{node} -p process.platform` failed",
            fix=f"check that `{node} --version` works, then re-run `computer mcp install`",
        )
    os_name, _, cpu = done.stdout.strip().partition(" ")
    return os_name, cpu


def _has_ripgrep(root: Path, platform: tuple[str, str]) -> bool:
    """Desktop Commander's ripgrep binary comes from an optional package, @vscode/ripgrep-<os>-<arch>, and @vscode/ripgrep
    loads only the one of node's own platform."""
    binary = "rg.exe" if platform[0] == "win32" else "rg"
    return (root / "node_modules" / "@vscode" / f"ripgrep-{platform[0]}-{platform[1]}" / "bin" / binary).is_file()


def _is_complete(root: Path, platform: tuple[str, str]) -> bool:
    """Whether a tree can serve: the entry file and the ripgrep binary search needs (the pre-lock layout is judged the same)."""
    return _entry_path(root).is_file() and _has_ripgrep(root, platform)


def _install_tree(npm: str, node: str, platform: tuple[str, str], package_json: bytes, lock: bytes, tree: Path) -> None:
    """Run npm ci in a staging directory and rename the finished tree to `tree`, a path nothing uses yet.

    npm ci deletes node_modules before it downloads anything, so it never runs in a tree a client may be launching: a failed or
    timed-out install leaves the working one untouched."""
    # One staging name per lock (not per repair generation), so whatever an attempt killed half way left behind is reclaimed
    # by the next one; the install lock keeps two attempts from sharing it.
    staging = _trees_dir() / f".staging-{tree.name[:64]}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        # npm ci installs exactly what the lock in the prefix says and refuses a package.json that disagrees with it.
        atomic_write(staging / "package.json", package_json.decode())
        atomic_write(staging / "package-lock.json", lock.decode())
        _run_npm(npm, node, platform, staging)
    except BaseException:
        # Nothing launches from staging; a partial tree left here would pile up across repair retries (each has its own name).
        shutil.rmtree(staging, ignore_errors=True)
        raise
    staging.rename(tree)


def _run_npm(npm: str, node: str, platform: tuple[str, str], prefix: Path) -> None:
    # npm's own shebang is `env node`, so the node we are about to record must be first on its PATH.
    env = {**os.environ, "PATH": f"{os.path.dirname(node)}{os.pathsep}{os.environ.get('PATH', '')}"}
    spec = f"{DESKTOP_COMMANDER_PACKAGE}@{DESKTOP_COMMANDER_VERSION}"
    command = [
        npm,
        "ci",
        "--ignore-scripts",
        "--omit=dev",
        # The layout the entry and ripgrep checks below expect, whatever the user's npmrc sets: hoisted, and with the optional
        # platform package that carries ripgrep even under `optional=false`.
        "--install-strategy=hoisted",
        "--include=optional",
        # Node's own platform, whatever npm_config_os/cpu or an npmrc says: @vscode/ripgrep loads no other platform's package.
        f"--os={platform[0]}",
        f"--cpu={platform[1]}",
        "--prefix",
        str(prefix),
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
    ]
    # Own session so an interruption can stop npm's whole process tree, not leave it writing into the prefix past the lock.
    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=_NPM_TIMEOUT_SECONDS)
    except BaseException as exc:
        # A timeout, Ctrl-C or any other way out stops npm's whole tree before the caller deletes the staging prefix and
        # releases the install lock; an npm left running would keep writing into the staging prefix a retry reuses.
        with suppress(ProcessLookupError):  # the tree may have exited between the interruption and here
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            raise NetworkError(
                what="npm ci timed out",
                why=f"npm did not finish within {_NPM_TIMEOUT_SECONDS}s installing {spec}",
                fix="check the network and re-run `computer mcp install`",
            ) from exc
        raise
    if proc.returncode != 0:
        output = (stderr or stdout).strip()
        # Classify on npm's own `code E...` line (free text such as "check your network" also appears in permission errors),
        # over the full output, and show only the tail.
        code = re.search(r"\bcode ([A-Z][A-Z_]+)\b", output)
        error_class = NetworkError if code is not None and code.group(1) in _NETWORK_CODES else AgentToolError
        raise error_class(
            what=f"npm ci of the locked {spec} tree failed",
            why=output[-600:] or f"npm exited {proc.returncode}",
            fix="fix the cause above and re-run `computer mcp install`",
        )
    if not _entry_path(prefix).is_file():
        raise AgentToolError(
            what="npm reported success but the Desktop Commander entry file is missing",
            why=f"expected {_entry_path(prefix)}",
            fix="remove the managed directory and re-run `computer mcp install`",
        )
    # npm ci drops an optional package whose download fails and still succeeds, which would leave search broken in a tree that
    # looks complete.
    if not _has_ripgrep(prefix, platform):
        raise NetworkError(
            what="npm ci left out Desktop Commander's ripgrep binary",
            why=f"no node_modules/@vscode/ripgrep-{'-'.join(platform)}/bin/rg under {prefix} (optional, npm skipped it)",
            fix="check the network and re-run `computer mcp install`; the working install, if any, is still in use",
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
    root = _tree_root(record)
    if root is None:
        raise MissingTargetError(what="the install record is damaged", why=f"no valid lock_sha256 in {_record_path()}", fix=fix)
    if not _entry_path(root).is_file():
        raise MissingTargetError(what="the Desktop Commander files are missing", why=f"no {_entry_path(root)}", fix=fix)
    return LaunchSpec(node=node, entry=_entry_path(root))
