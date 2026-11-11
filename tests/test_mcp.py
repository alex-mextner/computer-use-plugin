from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from computerlib.cli import main
from computerlib.mcp_probe import handshake
from computerlib.pins import DESKTOP_COMMANDER_PACKAGE, DESKTOP_COMMANDER_VERSION
from tests.fakes import fake_node, fake_npm, minimal_env

ENTRY = "node_modules/@wonderwhy-er/desktop-commander/dist/index.js"


def managed_dir(home: Path) -> Path:
    return home / ".local/share/computer/desktop-commander"


@pytest.fixture
def toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return bin_dir


def test_install_pins_exact_version_and_records_node(toolchain: Path, tmp_path: Path) -> None:
    assert main(["mcp", "install"]) == 0
    root = managed_dir(tmp_path / "home")
    assert (root / ENTRY).is_file()
    record = json.loads((root / "install.json").read_text())
    assert record["version"] == DESKTOP_COMMANDER_VERSION
    assert record["node"] == str(toolchain / "node")
    npm_args = (toolchain / "npm.log").read_text()
    assert f"{DESKTOP_COMMANDER_PACKAGE}@{DESKTOP_COMMANDER_VERSION}" in npm_args
    assert f"--prefix {root}" in npm_args
    assert "--save-exact" in npm_args


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
    assert (tmp_path / "xdg/computer/desktop-commander" / ENTRY).is_file()


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


def test_serve_execs_recorded_node_under_a_minimal_environment(toolchain: Path, tmp_path: Path) -> None:
    assert main(["mcp", "install"]) == 0
    home = tmp_path / "home"
    # env -i style: no PATH entry for node, so only the recorded absolute path can work.
    result = handshake(sys.executable, ["-m", "computerlib.cli", "mcp", "serve"], minimal_env(home), timeout=20)
    assert result.server_version == str(managed_dir(home) / ENTRY)


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
    assert (managed_dir(tmp_path / "home") / ENTRY).is_file()


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
