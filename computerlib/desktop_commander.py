"""The managed, pinned Desktop Commander install that every client adapter launches.

HOW IT IS REACHED: `computer mcp install` calls `install()`, `computer mcp serve` calls `launch_spec()`, and client adapters call
`install()` so their one command sets everything up.
INVARIANTS: one exact version from `pins.py` lives under `${XDG_DATA_HOME:-~/.local/share}/computer/desktop-commander`
(npm --prefix, never the floating npx cache), installed with `npm ci --ignore-scripts` from the package-lock.json bundled in
`computerlib/data/desktop-commander`, so every transitive package is the locked version with its integrity hash and a broken or
re-published tarball elsewhere on npm cannot change or break the install; `install.json` records the absolute `node` path that was
current at install time so launching needs no PATH (a desktop app autostarts with a bare one) and the sha256 of the lock it was
installed from; `install()` is idempotent and runs npm only when the pinned version or that lock is absent.
Each lock gets its own tree, `trees/<lock sha256>`, built by npm ci in a staging directory of its own per attempt and switched to
by rewriting `install.json`: npm ci deletes node_modules before downloading, so it never runs in the tree `serve` launches, and a
failed install leaves the working one in place. npm and puppeteer's browser download run in a session of their own that is
stopped as a whole on a timeout, Ctrl-C, SIGTERM or SIGHUP; an npm that outlived a SIGKILLed installer holds its staging
directory's `.writer.lock`, so that directory is never reused and is deleted only once nothing writes into it.
Older trees (and the node_modules of a pre-lock install) are never deleted by this code: a client started from one may still be
running and loading files from it; remove them by hand once clients restarted.
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
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import FrameType

from agenttools_errors import AgentToolError, MissingDepError, MissingTargetError, NetworkError

from computerlib import pdf_sandbox
from computerlib.fileio import atomic_write
from computerlib.jsonvalue import as_dict, is_object
from computerlib.paths import data_home
from computerlib.pins import DESKTOP_COMMANDER_PACKAGE, DESKTOP_COMMANDER_VERSION

# What a healthy Desktop Commander lists; the handshake in `status` checks for these.
EXPECTED_TOOLS = ("start_process", "read_file", "write_file")

_NPM_TIMEOUT_SECONDS = 900
# How often a running npm or browser download is checked for a held Ctrl-C or termination; also bounds how late one is acted on.
_POLL_SECONDS = 0.1
# How long a stop waits to reap its killed session before giving up on a child that left the session and keeps its pipes.
_REAP_GRACE_SECONDS = 5
# In each staging directory: held (flock) by the installer and inherited by its npm, so it stays locked while that npm lives.
_WRITER_LOCK = ".writer.lock"
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


class _Terminated(BaseException):
    """SIGTERM or SIGHUP turned into an exception, so the install unwinds (stopping npm's session) before the process dies."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class _Termination:
    """SIGTERM and SIGHUP of an install in the main thread, made to unwind like Ctrl-C does before the process dies of them,
    and, while a child session runs, those and Ctrl-C held back until the session can be stopped.

    Their default action kills Python without running a `finally`, and npm runs in a session of its own (so a stop can reach
    its whole tree), so it would keep writing into the staging prefix after the install lock is released. Only the main thread
    can set handlers, and a disposition the caller chose (ignored, its own handler) is left alone."""

    def __init__(self) -> None:
        self.taken: list[int] = []
        self.received: int | None = None
        self.holding = False
        self.pending: int | None = None
        self.interrupts = 0
        self.sigint_handler: Callable[[int, FrameType | None], object] | None = None

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        for each in self.taken:  # a repeated signal must not cut the cleanup short
            signal.signal(each, signal.SIG_IGN)
        self.received = signum
        if self.holding:
            self.pending = signum
            return
        raise _Terminated(signum)

    def _hold_interrupt(self, _signum: int, _frame: FrameType | None) -> None:
        self.interrupts += 1

    def _restore(self) -> None:
        for signum in self.taken:
            signal.signal(signum, signal.SIG_DFL)
        self.taken = []

    @contextmanager
    def unwinding(self) -> Generator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        self.received = None
        self.taken = [signum for signum in (signal.SIGTERM, signal.SIGHUP) if signal.getsignal(signum) is signal.SIG_DFL]
        for signum in self.taken:
            signal.signal(signum, self._handle)
        try:
            yield
        except BaseException:
            self._restore()
            if self.received is not None:
                # Exit status says "killed by SIGTERM", as without the handler, also when a later Ctrl-C ended the unwinding.
                signal.raise_signal(self.received)
            raise
        finally:
            self._restore()

    @contextmanager
    def held(self) -> Generator[None]:
        """Hold a termination or a Ctrl-C back while a child session runs; `deliver()` raises it where the session is in hand,
        and leaving the hold raises one still held. Raised anywhere else (inside Popen, inside the stop before the kill) it would
        leave the session running, and in a session of its own the child does not get the terminal's Ctrl-C itself. Not
        reentrant: entering resets what an earlier hold cut short by a signal may have left."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        self.pending, self.interrupts, self.sigint_handler = None, 0, None
        interrupt = signal.getsignal(signal.SIGINT)
        # Holding before the swap: a termination handled in between would otherwise leave SIGINT swapped for good.
        self.holding = True
        try:
            # Only a Python handler (KeyboardInterrupt by default) can be run later; an ignored SIGINT needs no holding, and a
            # default one kills this process without any cleanup either way.
            if callable(interrupt):
                self.sigint_handler = interrupt
                signal.signal(signal.SIGINT, self._hold_interrupt)
            yield
        finally:
            try:
                if callable(interrupt):
                    signal.signal(signal.SIGINT, interrupt)
            finally:
                self.holding = False
                self.deliver()

    def deliver(self) -> None:
        """Raise a termination held so far, else run the caller's SIGINT handler for a held Ctrl-C."""
        signum, self.pending = self.pending, None
        interrupted, self.interrupts = self.interrupts > 0, 0
        if signum is not None:
            raise _Terminated(signum)
        if interrupted and self.sigint_handler is not None:
            self.sigint_handler(signal.SIGINT, None)


_TERMINATION = _Termination()


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
    """Make the pinned install present and return its report, with a `warning:` line per problem it found. Raises
    AgentToolError (127 no node/npm, 7 network)."""
    report = _install_pinned()
    # Also for an install that was already there: the restriction and the profiles can change between runs.
    sandbox = pdf_sandbox.problem()
    return f"{report}\nwarning: {sandbox.detail}; fix: {sandbox.fix}" if sandbox else report


def _install_pinned() -> str:
    node = _real_node(_require("node"))
    platform = _node_platform(node)
    package_json, lock = bundled_lock_files()
    lock_sha256 = hashlib.sha256(lock).hexdigest()
    with _TERMINATION.unwinding(), _exclusive_install_lock():
        _reclaim_staging()
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
    env = _browser_env(node, tree)
    try:
        # Its own session, stopped as a whole: on a timeout the unzip child would otherwise keep writing the browser cache.
        done = _run_session([node, str(script)], env=env, cwd=tree, timeout=_NPM_TIMEOUT_SECONDS)
        # The script prints a failed download and still exits 0, so ask puppeteer whether its browser is really there.
        check = subprocess.run(
            [node, "-e", _BROWSER_PRESENT_JS, str(tree / "node_modules" / "puppeteer")],
            # The tree, not the caller's directory: puppeteer loads a `.puppeteerrc.cjs` from the working directory as code.
            cwd=tree,
            env=env,
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


def _node_first_env(node: str) -> dict[str, str]:
    """npm's own shebang is `env node`, so the node we are about to record must be first on the PATH of npm and its scripts."""
    return {**os.environ, "PATH": f"{os.path.dirname(node)}{os.pathsep}{os.environ.get('PATH', '')}"}


def _browser_env(node: str, tree: Path) -> dict[str, str]:
    """The environment npm gave puppeteer's postinstall: npm sets NODE_OPTIONS to its `node-options` setting (an npmrc's
    `node-options=--use-system-ca` behind a TLS-intercepting proxy) for scripts, which the direct node call would drop. Set, it
    replaces the caller's NODE_OPTIONS as under npm: appended, one of them could conflict with it (`--use-openssl-ca` and
    `--use-bundled-ca`) and node would refuse to start."""
    env = _node_first_env(node)
    found = shutil.which("npm")
    if found is None:  # a reused tree needs no npm; without one there is no npm setting to honour either
        return env
    try:
        # From the caller's directory with the tree as the prefix, as npm ci ran: a relative userconfig or globalconfig path
        # (NPM_CONFIG_USERCONFIG=.npmrc) resolves where the user meant it, and the project npmrc is the tree's (none), not one
        # of a project the caller happens to stand in. Like npm ci's, the default global npmrc is then the tree's too.
        done = subprocess.run(
            # Absolute, as `_require` made npm ci's: a bare name (an empty PATH entry) would be looked up again on node's PATH.
            [os.path.abspath(found), "config", "get", "node-options", "--prefix", str(tree)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return env  # the download may well work without it; if not, its own warning says so
    configured = done.stdout.strip()
    if done.returncode == 0 and configured not in ("", "null", "undefined"):  # npm prints `null` for the unset default
        env["NODE_OPTIONS"] = configured
    return env


def _run_session(
    command: Sequence[str], *, env: dict[str, str], cwd: Path | None, timeout: float, pass_fds: Sequence[int] = ()
) -> subprocess.CompletedProcess[str]:
    """Run `command` in a session of its own and stop that whole session on any way out: a timeout, Ctrl-C, SIGTERM.

    npm and puppeteer's download start children, and killing only the direct child would leave them writing (into the
    staging prefix, into the browser cache) after the install lock is released. Ctrl-C and terminations are held for the whole
    run and raised only between polls, where the session is in hand: raised inside Popen or inside the stop before the kill,
    they would leave it running. Raises subprocess.TimeoutExpired."""
    deadline = time.monotonic() + timeout
    with _TERMINATION.held():
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            pass_fds=pass_fds,
        )
        try:
            while True:
                try:
                    stdout, stderr = proc.communicate(timeout=min(_POLL_SECONDS, max(deadline - time.monotonic(), 0)))
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, timeout) from None
                else:
                    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
                # Not in the handler above: a Ctrl-C would then show the poll's timeout as what it interrupted.
                _TERMINATION.deliver()
        except BaseException:
            _stop_session(proc)
            raise


def _stop_session(proc: subprocess.Popen[str]) -> None:
    """Kill the whole session and reap its leader, still held, so nothing of it writes once the caller cleans up.

    A reap that never ends (a child that left the session keeps the pipes open) ends at the next Ctrl-C or after a grace
    period: the session is killed by then, waiting longer gains nothing, and an unattended install (no Ctrl-C can come) would
    otherwise hold the install lock, and so block every later install, for good."""
    with suppress(ProcessLookupError):  # the session may have exited between the interruption and here
        os.killpg(proc.pid, signal.SIGKILL)
    interrupts, grace_ends = _TERMINATION.interrupts, time.monotonic() + _REAP_GRACE_SECONDS
    while True:
        with suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_POLL_SECONDS)
            return
        if _TERMINATION.interrupts != interrupts or time.monotonic() >= grace_ends:
            return


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
    # A new staging directory per attempt: an npm that outlived a SIGKILLed installer may still be writing into its own, and
    # the install lock no longer covers it. `_reclaim_staging` deletes such a directory once its npm is gone.
    staging = _trees_dir() / f".staging-{tree.name[:64]}-{os.getpid()}-{time.time_ns()}"
    staging.mkdir(parents=True)
    try:
        with open(staging / _WRITER_LOCK, "w") as writer:
            fcntl.flock(writer, fcntl.LOCK_EX)
            # npm ci installs exactly what the lock in the prefix says and refuses a package.json that disagrees with it.
            atomic_write(staging / "package.json", package_json.decode())
            atomic_write(staging / "package-lock.json", lock.decode())
            _run_npm(npm, node, platform, staging, writer.fileno())
        (staging / _WRITER_LOCK).unlink()
    except BaseException:
        # Nothing launches from staging; a partial tree left here would pile up across retries (each has its own name).
        shutil.rmtree(staging, ignore_errors=True)
        raise
    staging.rename(tree)


def _reclaim_staging() -> None:
    """Delete the staging directories killed attempts left behind, except one an npm still writes into (it holds the lock)."""
    if not _trees_dir().is_dir():
        return
    for staging in _trees_dir().iterdir():
        if not staging.name.startswith(".staging-") or not staging.is_dir():
            continue
        # The lock file is created for a directory left by a version without it. BlockingIOError: an npm that outlived its
        # killed installer still holds it; the directory is never activated, so it is left to that npm until a later install.
        with suppress(OSError), open(staging / _WRITER_LOCK, "a") as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            shutil.rmtree(staging, ignore_errors=True)


def _run_npm(npm: str, node: str, platform: tuple[str, str], prefix: Path, writer_lock: int) -> None:
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
    try:
        # npm inherits the staging directory's writer lock, so the directory stays locked for as long as npm (or a child of
        # it) runs, even past a SIGKILL of this process.
        done = _run_session(command, env=_node_first_env(node), cwd=None, timeout=_NPM_TIMEOUT_SECONDS, pass_fds=(writer_lock,))
    except subprocess.TimeoutExpired as exc:
        raise NetworkError(
            what="npm ci timed out",
            why=f"npm did not finish within {_NPM_TIMEOUT_SECONDS}s installing {spec}",
            fix="check the network and re-run `computer mcp install`",
        ) from exc
    if done.returncode != 0:
        output = (done.stderr or done.stdout).strip()
        # Classify on npm's own `code E...` line (free text such as "check your network" also appears in permission errors),
        # over the full output, and show only the tail.
        code = re.search(r"\bcode ([A-Z][A-Z_]+)\b", output)
        error_class = NetworkError if code is not None and code.group(1) in _NETWORK_CODES else AgentToolError
        raise error_class(
            what=f"npm ci of the locked {spec} tree failed",
            why=output[-600:] or f"npm exited {done.returncode}",
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
