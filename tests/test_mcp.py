from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import pytest

from computerlib.cli import main
from computerlib.desktop_commander import bundled_lock_files, install, launch_spec
from computerlib.mcp_probe import handshake
from computerlib.pins import DESKTOP_COMMANDER_PACKAGE, DESKTOP_COMMANDER_VERSION
from tests.fakes import fake_node, fake_npm, minimal_env

ENTRY = "node_modules/@wonderwhy-er/desktop-commander/dist/index.js"
LOCK_SHA256 = hashlib.sha256(bundled_lock_files()[1]).hexdigest()


def managed_dir(home: Path) -> Path:
    return home / ".local/share/computer/desktop-commander"


def tree_dir(home: Path) -> Path:
    """Where the install from the bundled lock lives."""
    return managed_dir(home) / "trees" / LOCK_SHA256


@pytest.fixture
def toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return bin_dir


def test_install_runs_npm_ci_from_the_bundled_lock_and_records_node(toolchain: Path, tmp_path: Path) -> None:
    assert main(["mcp", "install"]) == 0
    root = managed_dir(tmp_path / "home")
    tree = tree_dir(tmp_path / "home")
    assert (tree / ENTRY).is_file()
    package_json, lock = bundled_lock_files()
    record = json.loads((root / "install.json").read_text())
    assert record["version"] == DESKTOP_COMMANDER_VERSION
    assert record["lock_sha256"] == LOCK_SHA256
    assert record["node"] == str(toolchain / "node")
    npm_args = (toolchain / "npm.log").read_text().split()
    assert npm_args[:2] == ["ci", "--ignore-scripts"]
    # Hoisted whatever the user's npmrc says, so the platform ripgrep package sits where the activation check looks.
    assert "--install-strategy=hoisted" in npm_args and "--include=optional" in npm_args
    # Only puppeteer's browser download runs, with the recorded node, in the finished tree.
    assert (toolchain / "node.log").read_text().split() == [str(tree), str(tree / "node_modules/puppeteer/install.mjs")]
    # npm ci ran in a staging directory of this attempt that then became the tree, never in a tree a client may be launching.
    assert npm_args[npm_args.index("--prefix") + 1].startswith(str(root / "trees" / f".staging-{LOCK_SHA256}-"))
    assert sorted(path.name for path in (root / "trees").iterdir()) == [LOCK_SHA256]
    # npm ci installed from exactly the bundled lock, next to the bundled package.json it must agree with.
    assert (toolchain / "npm-lock.json").read_bytes() == lock
    assert (tree / "package.json").read_bytes() == package_json


@pytest.mark.parametrize("failure", [{"FAKE_BROWSER_EXIT": "1"}, {"FAKE_BROWSER_MISSING": "1"}])
def test_a_failed_browser_download_only_warns(
    toolchain: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: dict[str, str],
) -> None:
    # Both the script failing and the script exiting 0 after a swallowed download error (no browser afterwards) count.
    for name, value in failure.items():
        monkeypatch.setenv(name, value)
    assert main(["mcp", "install"]) == 0
    out = capsys.readouterr().out
    assert "warning: the browser for PDF output was not downloaded (browser download failed)" in out
    # The command it names runs from the tree: puppeteer loads a `.puppeteerrc.cjs` from the working directory as code.
    assert f"re-run `cd {tree_dir(tmp_path / 'home')} && " in out
    assert "install.mjs" in out
    assert launch_spec().entry == tree_dir(tmp_path / "home") / ENTRY


def test_the_bundled_lock_pins_desktop_commander_and_every_package_below_it() -> None:
    package_json, lock = bundled_lock_files()
    pinned = {DESKTOP_COMMANDER_PACKAGE: DESKTOP_COMMANDER_VERSION}
    assert json.loads(package_json)["dependencies"] == pinned
    packages = json.loads(lock)["packages"]
    assert packages[""]["dependencies"] == pinned
    assert packages[f"node_modules/{DESKTOP_COMMANDER_PACKAGE}"]["version"] == DESKTOP_COMMANDER_VERSION
    for name, entry in packages.items():
        if name:
            assert entry["resolved"].startswith("https://registry.npmjs.org/"), name
            assert entry["integrity"].startswith("sha512-"), name
    # puppeteer-core 25.18.1 was listed by npm while its tarball answered 404 (#11): the lock holds the previous release.
    assert packages["node_modules/puppeteer-core"]["version"] == "25.18.0"
    assert "puppeteer-core-25.18.1.tgz" not in lock.decode()


def test_an_install_from_another_lock_is_replaced_by_the_bundled_one(toolchain: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    other = "0" * 64
    _older_tree(home, other)
    record = {"package": DESKTOP_COMMANDER_PACKAGE, "version": DESKTOP_COMMANDER_VERSION, "lock_sha256": other, "tree": other}
    (managed_dir(home) / "install.json").write_text(json.dumps({**record, "node": str(toolchain / "node")}))
    assert main(["mcp", "install"]) == 0
    assert len((toolchain / "npm.log").read_text().splitlines()) == 1
    assert json.loads((managed_dir(home) / "install.json").read_text())["lock_sha256"] == LOCK_SHA256
    assert launch_spec().entry == tree_dir(home) / ENTRY


def _pre_lock_install(home: Path, node: Path) -> dict[str, object]:
    """What 0.2.0 left behind: `npm install --prefix` straight into the managed directory and a record without a lock."""
    entry = managed_dir(home) / ENTRY
    entry.parent.mkdir(parents=True)
    entry.write_text("// pre-lock dc")
    record: dict[str, object] = {"package": DESKTOP_COMMANDER_PACKAGE, "version": DESKTOP_COMMANDER_VERSION, "node": str(node)}
    (managed_dir(home) / "install.json").write_text(json.dumps(record))
    return record


def test_a_failed_migration_leaves_the_pre_lock_install_launchable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    node = fake_node(bin_dir)
    fake_npm(bin_dir, mode="fail", fail_with="npm error code ECONNRESET")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    home = tmp_path / "home"
    record = _pre_lock_install(home, node)
    assert main(["mcp", "install"]) == 7
    assert json.loads((managed_dir(home) / "install.json").read_text()) == record
    assert launch_spec().entry == managed_dir(home) / ENTRY
    assert launch_spec().entry.read_text() == "// pre-lock dc"


def test_a_migration_serves_the_old_tree_until_the_new_one_is_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    node = fake_node(bin_dir)
    old_entry = managed_dir(home) / ENTRY
    # While npm runs, record what `serve` would resolve: the old record and its entry file must still be there.
    seen = tmp_path / "seen-during-npm"
    watch = f'cp "{managed_dir(home)}/install.json" "{seen}.json"; test -f "{old_entry}" && touch "{seen}.ok"'
    fake_npm(bin_dir, side_effect=watch)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    record = _pre_lock_install(home, node)
    assert main(["mcp", "install"]) == 0
    assert json.loads(Path(f"{seen}.json").read_text()) == record
    assert Path(f"{seen}.ok").exists()
    assert launch_spec().entry == tree_dir(home) / ENTRY
    # A client started before the switch may still be running from the pre-lock files, so they are left in place.
    assert old_entry.read_text() == "// pre-lock dc"


def _older_tree(home: Path, name: str) -> Path:
    entry = managed_dir(home) / "trees" / name / ENTRY
    entry.parent.mkdir(parents=True)
    entry.write_text("// older dc")
    return entry


def test_an_upgrade_leaves_every_older_tree_in_place(toolchain: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    older = [_older_tree(home, name * 64) for name in "ab"]
    record = {
        "package": DESKTOP_COMMANDER_PACKAGE,
        "version": DESKTOP_COMMANDER_VERSION,
        "lock_sha256": "a" * 64,
        "tree": "a" * 64,
    }
    (managed_dir(home) / "install.json").write_text(json.dumps({**record, "node": str(toolchain / "node")}))
    assert main(["mcp", "install"]) == 0
    assert launch_spec().entry == tree_dir(home) / ENTRY
    assert all(entry.read_text() == "// older dc" for entry in older)


def test_a_lost_record_reuses_the_complete_tree_without_npm(toolchain: Path, tmp_path: Path) -> None:
    assert main(["mcp", "install"]) == 0
    (managed_dir(tmp_path / "home") / "install.json").unlink()
    assert main(["mcp", "install"]) == 0
    assert len((toolchain / "npm.log").read_text().splitlines()) == 1
    assert launch_spec().entry == tree_dir(tmp_path / "home") / ENTRY
    # An install interrupted after promoting the tree never reached the browser download, so a reuse runs it too.
    assert len((toolchain / "node.log").read_text().splitlines()) == 2


def test_a_lost_record_with_an_incomplete_tree_rebuilds_it_as_a_new_generation(toolchain: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert main(["mcp", "install"]) == 0
    (managed_dir(home) / "install.json").unlink()
    (tree_dir(home) / "node_modules/@vscode/ripgrep-linux-x64/bin/rg").unlink()
    assert main(["mcp", "install"]) == 0
    assert len((toolchain / "npm.log").read_text().splitlines()) == 2
    assert launch_spec().entry.parents[4].name.startswith(f"{LOCK_SHA256}-")


def test_a_damaged_tree_is_repaired_as_a_new_generation_and_left_in_place(toolchain: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert main(["mcp", "install"]) == 0
    (tree_dir(home) / ENTRY).unlink()
    (tree_dir(home) / "marker").write_text("old tree")
    assert main(["mcp", "install"]) == 0
    assert len((toolchain / "npm.log").read_text().splitlines()) == 2
    # A client started from the damaged tree keeps finding every file it still has at the same path.
    assert (tree_dir(home) / "marker").read_text() == "old tree"
    repaired = launch_spec().entry
    assert repaired.is_file() and repaired.is_relative_to(managed_dir(home) / "trees")
    assert repaired.parents[4].name.startswith(f"{LOCK_SHA256}-")
    assert main(["mcp", "install"]) == 0  # the repaired generation counts as installed
    assert len((toolchain / "npm.log").read_text().splitlines()) == 2


def test_failed_repairs_leave_no_partial_staging_tree(toolchain: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert main(["mcp", "install"]) == 0
    (tree_dir(home) / ENTRY).unlink()
    # npm extracts part of the tree into the staging prefix, then fails.
    partial = 'mkdir -p "$prefix/node_modules/partial"; echo "npm error code ECONNRESET" >&2; exit 1'
    fake_npm(toolchain, side_effect=partial.replace("$prefix", '$(echo "$@" | sed "s/.*--prefix \\([^ ]*\\).*/\\1/")'))
    assert main(["mcp", "install"]) == 7
    assert main(["mcp", "install"]) == 7
    assert sorted(path.name for path in (managed_dir(home) / "trees").iterdir()) == [LOCK_SHA256]


def test_a_repair_reclaims_the_staging_left_by_a_killed_attempt(toolchain: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert main(["mcp", "install"]) == 0
    (tree_dir(home) / ENTRY).unlink()
    leftover = managed_dir(home) / "trees" / f".staging-{LOCK_SHA256}" / "node_modules" / "partial"
    leftover.mkdir(parents=True)
    assert main(["mcp", "install"]) == 0
    assert not any(path.name.startswith(".staging-") for path in (managed_dir(home) / "trees").iterdir())


def test_a_tree_without_the_ripgrep_binary_is_not_activated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    node = fake_node(bin_dir)
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_NPM_SKIP_RIPGREP", "1")  # npm ci succeeds but dropped the optional platform package
    record = _pre_lock_install(home, node)
    assert main(["mcp", "install"]) == 7
    assert json.loads((managed_dir(home) / "install.json").read_text()) == record
    assert launch_spec().entry == managed_dir(home) / ENTRY
    monkeypatch.delenv("FAKE_NPM_SKIP_RIPGREP")
    assert main(["mcp", "install"]) == 0  # a retry is not fooled into "already installed"
    assert launch_spec().entry == tree_dir(home) / ENTRY


def test_an_npm_platform_override_does_not_pick_the_ripgrep_package(
    toolchain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("npm_config_os", "darwin")  # like `os=darwin` in an npmrc: a package this node never loads
    monkeypatch.setenv("npm_config_cpu", "arm64")
    assert main(["mcp", "install"]) == 0
    rg_packages = [path.name for path in (tree_dir(tmp_path / "home") / "node_modules" / "@vscode").iterdir()]
    assert rg_packages == ["ripgrep-linux-x64"]
    assert "--os=linux --cpu=x64" in (toolchain / "npm.log").read_text()


@pytest.mark.parametrize("damage", ["other_platform", "renamed", "directory", "dangling_symlink"])
def test_a_tree_without_this_platforms_ripgrep_binary_is_not_complete(toolchain: Path, tmp_path: Path, damage: str) -> None:
    home = tmp_path / "home"
    assert main(["mcp", "install"]) == 0
    vscode = tree_dir(home) / "node_modules" / "@vscode"
    rg = vscode / "ripgrep-linux-x64" / "bin" / "rg"
    if damage == "other_platform":
        (vscode / "ripgrep-linux-x64").rename(vscode / "ripgrep-darwin-x64")
    elif damage == "renamed":
        rg.rename(rg.with_name("rg.bak"))
    elif damage == "directory":
        rg.unlink()
        rg.mkdir()
    else:
        rg.unlink()
        rg.symlink_to(tmp_path / "missing")
    assert main(["mcp", "install"]) == 0  # not "already installed": search would fail from this tree
    assert len((toolchain / "npm.log").read_text().splitlines()) == 2
    assert launch_spec().entry.parents[4].name.startswith(f"{LOCK_SHA256}-")


def test_an_interrupted_install_stops_npm_before_cleaning_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    pid_file = tmp_path / "npm.pid"
    fake_npm(bin_dir, mode="hang", side_effect=f'echo $$ > "{pid_file}"')
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    def ctrl_c_once_npm_runs() -> None:
        while not pid_file.exists():
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)  # what a terminal's Ctrl-C delivers to the installing process

    # Ctrl-C must arrive as KeyboardInterrupt, whatever handler an earlier test left behind.
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    request.addfinalizer(lambda: signal.signal(signal.SIGINT, previous))
    threading.Thread(target=ctrl_c_once_npm_runs, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        install()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert not any((managed_dir(tmp_path / "home") / "trees").iterdir())


def _running(pid: int) -> bool:
    """Alive and not a zombie (a killed orphan waits for init to reap it, and a zombie writes nothing)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _eventually(condition: Callable[[], bool], seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def _start_installer(bin_dir: Path, prelude: str = "") -> subprocess.Popen[bytes]:
    """`computer mcp install` in a process of its own (it is to die of a signal), `bin_dir` on PATH, `prelude` run first."""
    return subprocess.Popen(
        [sys.executable, "-c", f"{prelude}\nimport sys\nfrom computerlib.cli import main\nsys.exit(main(['mcp', 'install']))"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _installer_mid_npm(tmp_path: Path) -> tuple[subprocess.Popen[bytes], int, int, Path]:
    """A `computer mcp install` process whose npm ci (with a child of its own) hangs: (installer, npm pid, child pid, prefix)."""
    bin_dir = tmp_path / "hanging"
    fake_node(bin_dir)
    state = tmp_path / "npm.state"
    fake_npm(
        bin_dir,
        mode="hang",
        side_effect=f'/bin/sleep 60 & echo "$$ $! $prefix" > "{state}.tmp" && mv "{state}.tmp" "{state}"',
    )
    installer = _start_installer(bin_dir)
    assert _eventually(state.exists), "npm ci never started"
    npm_pid, child_pid, prefix = state.read_text().split()
    return installer, int(npm_pid), int(child_pid), Path(prefix)


def _stop_session(leader: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(leader, signal.SIGKILL)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_terminated_install_stops_npms_whole_session_and_a_retry_starts_clean(
    toolchain: Path, tmp_path: Path, signum: signal.Signals
) -> None:
    installer, npm_pid, child_pid, prefix = _installer_mid_npm(tmp_path)
    installer.send_signal(signum)
    # It still dies of the signal (a service manager sees what it sent), only after stopping npm and cleaning up.
    assert installer.wait(timeout=30) == -signum
    assert not _running(npm_pid)
    assert _eventually(lambda: not _running(child_pid))
    assert not prefix.exists()
    assert main(["mcp", "install"]) == 0  # at once, with a working npm (the toolchain on PATH)
    assert sorted(path.name for path in (managed_dir(tmp_path / "home") / "trees").iterdir()) == [LOCK_SHA256]


def test_a_retry_after_a_killed_installer_never_shares_the_staging_its_npm_still_writes(
    toolchain: Path, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    installer, npm_pid, _, orphan_prefix = _installer_mid_npm(tmp_path)
    request.addfinalizer(lambda: _stop_session(npm_pid))
    installer.kill()  # SIGKILL runs no cleanup at all: npm, in a session of its own, lives on
    installer.wait(timeout=30)
    assert _running(npm_pid)
    assert main(["mcp", "install"]) == 0  # at once, while the orphan still runs
    npm_args = (toolchain / "npm.log").read_text().split()
    retry_prefix = Path(npm_args[npm_args.index("--prefix") + 1])
    assert retry_prefix != orphan_prefix
    assert retry_prefix.parent == orphan_prefix.parent
    assert orphan_prefix.is_dir()  # not deleted under the orphan, and never activated
    assert launch_spec().entry == tree_dir(tmp_path / "home") / ENTRY
    _stop_session(npm_pid)
    assert _eventually(lambda: not _running(npm_pid))
    assert main(["mcp", "install"]) == 0  # nothing writes there any more, so it is reclaimed
    assert sorted(path.name for path in (managed_dir(tmp_path / "home") / "trees").iterdir()) == [LOCK_SHA256]


def test_a_termination_the_moment_npm_starts_still_stops_it(tmp_path: Path) -> None:
    bin_dir = tmp_path / "hanging"
    fake_node(bin_dir)
    fake_npm(bin_dir, mode="hang")
    pid_file = tmp_path / "npm.pid"
    # SIGTERM handled inside Popen, right after npm ci started: before the caller holds the process it has to stop.
    prelude = (
        "import os, signal, subprocess\n"
        "real_popen = subprocess.Popen\n"
        "def popen(args, *rest, **kwargs):\n"
        "    proc = real_popen(args, *rest, **kwargs)\n"
        "    if 'ci' in args:\n"
        f"        with open({str(pid_file)!r}, 'w') as out:\n"
        "            out.write(str(proc.pid))\n"
        "        signal.raise_signal(signal.SIGTERM)\n"
        "    return proc\n"
        "subprocess.Popen = popen\n"
    )
    installer = _start_installer(bin_dir, prelude)
    assert installer.wait(timeout=30) == -signal.SIGTERM
    npm_pid = int(pid_file.read_text())
    orphaned = _running(npm_pid)
    _stop_session(npm_pid)  # a failing run must not leave the orphan behind
    assert not orphaned
    assert not any(path.name.startswith(".staging-") for path in (managed_dir(tmp_path / "home") / "trees").iterdir())


def test_a_timed_out_browser_download_stops_its_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = tmp_path / "bin"
    unzip = tmp_path / "unzip.pid"
    # Like puppeteer's download: node starts an unzip child, and the download outlasts the timeout.
    fake_node(bin_dir, browser_side_effect=f'/bin/sleep 60 & echo $! > "{unzip}"; exec /bin/sleep 60')
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr("computerlib.desktop_commander._NPM_TIMEOUT_SECONDS", 2)
    assert main(["mcp", "install"]) == 0
    assert "warning: the browser for PDF output was not downloaded" in capsys.readouterr().out
    assert _eventually(lambda: not _running(int(unzip.read_text())))


@pytest.mark.parametrize(
    ("caller", "npm_setting", "expected"),
    [
        ("", "--use-system-ca", "--use-system-ca"),
        ("--max-old-space-size=4096", "--use-system-ca", "--use-system-ca"),  # npm replaces it: options could conflict
        ("--max-old-space-size=4096", "null", "--max-old-space-size=4096"),  # npm's unset default
        ("", "null", ""),
    ],
)
def test_npms_node_options_reach_the_browser_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str, npm_setting: str, expected: str
) -> None:
    bin_dir = tmp_path / "bin"
    seen = tmp_path / "node-options"
    fake_node(bin_dir, browser_side_effect=f'echo "$NODE_OPTIONS" > "{seen}"')
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_NPM_CONFIG", npm_setting)  # what `node-options=` in an npmrc makes npm answer
    if caller:
        monkeypatch.setenv("NODE_OPTIONS", caller)
    else:
        monkeypatch.delenv("NODE_OPTIONS", raising=False)
    assert main(["mcp", "install"]) == 0
    assert seen.read_text().strip() == expected
    # Asked from the tree, where puppeteer's postinstall ran under npm.
    tree = tree_dir(tmp_path / "home")
    assert (bin_dir / "npm-config.log").read_text().split() == [str(tree), "config", "get", "node-options"]


def test_npms_node_options_are_found_through_a_relative_path_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    seen = tmp_path / "node-options"
    fake_node(bin_dir, browser_side_effect=f'echo "$NODE_OPTIONS" > "{seen}"')
    fake_npm(bin_dir)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "bin:/usr/bin:/bin")  # npm config get runs from the tree, where `bin` is not
    monkeypatch.setenv("FAKE_NPM_CONFIG", "--use-system-ca")
    assert main(["mcp", "install"]) == 0
    assert seen.read_text().strip() == "--use-system-ca"


@pytest.mark.parametrize("tree", ["../../elsewhere", None, "a" * 64 + "/.."])
def test_serve_exits_5_when_the_recorded_tree_is_not_a_tree_name(toolchain: Path, tmp_path: Path, tree: str | None) -> None:
    assert main(["mcp", "install"]) == 0
    record_path = managed_dir(tmp_path / "home") / "install.json"
    record_path.write_text(json.dumps({**json.loads(record_path.read_text()), "tree": tree}))
    assert main(["mcp", "serve"]) == 5


def test_install_is_idempotent(toolchain: Path) -> None:
    assert main(["mcp", "install"]) == 0
    assert main(["mcp", "install"]) == 0
    assert len((toolchain / "npm.log").read_text().splitlines()) == 1


def test_install_refreshes_recorded_node_without_reinstalling(
    toolchain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["mcp", "install"]) == 0
    other = tmp_path / "other"
    fake_node(other)
    monkeypatch.setenv("PATH", f"{other}:{toolchain}:/usr/bin:/bin")
    assert main(["mcp", "install"]) == 0
    record = json.loads((managed_dir(tmp_path / "home") / "install.json").read_text())
    assert record["node"] == str(other / "node")
    assert len((toolchain / "npm.log").read_text().splitlines()) == 1


def test_install_honours_xdg_data_home(toolchain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert main(["mcp", "install"]) == 0
    assert (tmp_path / "xdg/computer/desktop-commander/trees" / LOCK_SHA256 / ENTRY).is_file()


def test_install_without_node_exits_127(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert main(["mcp", "install"]) == 127


def test_install_without_npm_exits_127(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert main(["mcp", "install"]) == 127


def test_install_network_failure_exits_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir, mode="fail", fail_with="npm error code ENOTFOUND")
    monkeypatch.setenv("PATH", str(bin_dir))
    assert main(["mcp", "install"]) == 7
    assert not (managed_dir(tmp_path / "home") / "install.json").exists()


def test_install_other_npm_failure_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir, mode="fail", fail_with="npm error code EACCES")
    monkeypatch.setenv("PATH", str(bin_dir))
    assert main(["mcp", "install"]) == 1


def test_serve_without_install_exits_5_with_fix(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mcp", "serve"]) == 5
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "computer mcp install" in captured.err


def test_serve_with_stale_pin_exits_5(toolchain: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mcp", "install"]) == 0
    record_path = managed_dir(tmp_path / "home") / "install.json"
    record = json.loads(record_path.read_text())
    record["version"] = "0.0.1"
    record_path.write_text(json.dumps(record))
    assert main(["mcp", "serve"]) == 5
    assert "computer mcp install" in capsys.readouterr().err


def test_serve_runs_recorded_node_under_a_minimal_environment(toolchain: Path, tmp_path: Path) -> None:
    assert main(["mcp", "install"]) == 0
    home = tmp_path / "home"
    # env -i style: no PATH entry for node, so only the recorded absolute path can work.
    result = handshake(sys.executable, ["-m", "computerlib.cli", "mcp", "serve"], minimal_env(home), timeout=20)
    # The stub echoes its argv: the entry file, then the switch that keeps a first run from waiting on a browser (#21).
    assert result.server_version == f"{tree_dir(home) / ENTRY} --no-onboarding"


def test_install_records_the_binary_node_reports_not_the_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_node = fake_node(tmp_path / "real")
    shim_dir = tmp_path / "shims"
    fake_node(shim_dir, exec_path=real_node)
    fake_npm(shim_dir)
    monkeypatch.setenv("PATH", f"{shim_dir}:/usr/bin:/bin")
    assert main(["mcp", "install"]) == 0
    record = json.loads((managed_dir(tmp_path / "home") / "install.json").read_text())
    assert record["node"] == str(real_node)


def test_permission_failure_mentioning_the_network_is_not_a_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir, mode="fail", fail_with="npm error code EACCES\nnpm error check your network settings")
    monkeypatch.setenv("PATH", str(bin_dir))
    assert main(["mcp", "install"]) == 1


def test_concurrent_installs_run_npm_once(toolchain: Path) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [pool.submit(main, ["mcp", "install"]) for _ in range(2)]
        codes = [run.result() for run in runs]
    assert codes == [0, 0]
    assert len((toolchain / "npm.log").read_text().splitlines()) == 1


def test_relative_xdg_data_home_is_ignored(toolchain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    assert main(["mcp", "install"]) == 0
    assert (tree_dir(tmp_path / "home") / ENTRY).is_file()


def test_install_npm_timeout_exits_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir, mode="hang")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr("computerlib.desktop_commander._NPM_TIMEOUT_SECONDS", 0.5)
    assert main(["mcp", "install"]) == 7


def test_install_detects_network_failure_even_when_npm_output_is_long(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir, mode="fail", fail_with="npm error code ECONNRESET\\n" + "x" * 2000)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert main(["mcp", "install"]) == 7


def test_install_fails_when_npm_succeeds_without_the_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir, mode="no_files")
    monkeypatch.setenv("PATH", str(bin_dir))
    assert main(["mcp", "install"]) == 1
    assert not (managed_dir(tmp_path / "home") / "install.json").exists()


def test_serve_exits_5_when_recorded_node_is_gone(toolchain: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mcp", "install"]) == 0
    (toolchain / "node").unlink()
    assert main(["mcp", "serve"]) == 5
    assert "computer mcp install" in capsys.readouterr().err


@pytest.mark.parametrize("garbage", ["not json", "[1, 2]", ""])
def test_serve_exits_5_when_install_record_is_unreadable(toolchain: Path, tmp_path: Path, garbage: str) -> None:
    assert main(["mcp", "install"]) == 0
    (managed_dir(tmp_path / "home") / "install.json").write_text(garbage)
    assert main(["mcp", "serve"]) == 5


def test_install_repairs_an_unreadable_record(toolchain: Path, tmp_path: Path) -> None:
    assert main(["mcp", "install"]) == 0
    record_path = managed_dir(tmp_path / "home") / "install.json"
    record_path.write_text("not json")
    assert main(["mcp", "install"]) == 0
    assert json.loads(record_path.read_text())["version"] == DESKTOP_COMMANDER_VERSION


def test_install_keeps_a_symlinked_node_name_so_an_upgrade_does_not_strand_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_node = fake_node(tmp_path / "cellar")
    stable = tmp_path / "bin"
    stable.mkdir()
    (stable / "node").symlink_to(real_node)
    fake_npm(stable)
    monkeypatch.setenv("PATH", f"{stable}:/usr/bin:/bin")
    # node reports its resolved binary; the symlink resolves to the same file, so it is not a shim.
    fake_node(tmp_path / "cellar", exec_path=real_node)
    assert main(["mcp", "install"]) == 0
    record = json.loads((managed_dir(tmp_path / "home") / "install.json").read_text())
    assert record["node"] == str(stable / "node")


def test_install_rejects_a_node_that_is_too_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir, version="12.22.9")
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert main(["mcp", "install"]) == 127
    assert not (bin_dir / "npm.log").exists()


@pytest.mark.parametrize("code", ["EPROTO", "ERR_SOCKET_TIMEOUT", "EHOSTUNREACH", "UNABLE_TO_GET_ISSUER_CERT_LOCALLY"])
def test_other_network_failure_codes_exit_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir, mode="fail", fail_with=f"npm error code {code}")
    monkeypatch.setenv("PATH", str(bin_dir))
    assert main(["mcp", "install"]) == 7


def test_reinstall_does_not_need_npm_when_nothing_is_installed(toolchain: Path) -> None:
    assert main(["mcp", "install"]) == 0
    (toolchain / "npm").unlink()
    assert main(["mcp", "install"]) == 0
