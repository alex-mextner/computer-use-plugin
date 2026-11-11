from __future__ import annotations

import sys
from pathlib import Path

import pytest
from agenttools_errors import MissingTargetError

from computerlib.executable import computer_executable
from tests.fakes import write_executable


def test_uses_the_invoked_path_without_resolving_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = write_executable(tmp_path / "tool/venv/bin/computer", "#!/bin/sh\n")
    link = tmp_path / "bin/computer"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(sys, "argv", [str(link), "claude"])
    assert computer_executable() == link


def test_falls_back_to_path_lookup_when_not_started_as_computer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    on_path = write_executable(tmp_path / "bin/computer", "#!/bin/sh\n")
    monkeypatch.setenv("PATH", f"{on_path.parent}:/usr/bin:/bin")
    monkeypatch.setattr(sys, "argv", ["python -m pytest"])
    assert computer_executable() == on_path


def test_falls_back_to_the_interpreters_bin_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv_bin = tmp_path / "venv/bin"
    write_executable(venv_bin / "python", "#!/bin/sh\n")
    sibling = write_executable(venv_bin / "computer", "#!/bin/sh\n")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    assert computer_executable() == sibling


def test_exits_5_when_nothing_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere/python"))
    with pytest.raises(MissingTargetError):
        computer_executable()
