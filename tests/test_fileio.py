from __future__ import annotations

from pathlib import Path

from computerlib.fileio import atomic_write, backup


def test_atomic_write_keeps_the_existing_mode(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("old")
    target.chmod(0o600)
    atomic_write(target, "new")
    assert target.read_text() == "new"
    assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_creates_missing_parents_and_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "a/b/config.json"
    atomic_write(target, "x")
    assert target.read_text() == "x"
    assert [p.name for p in target.parent.iterdir()] == ["config.json"]


def test_atomic_write_writes_through_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles/config.json"
    real.parent.mkdir()
    real.write_text("old")
    link = tmp_path / "config.json"
    link.symlink_to(real)
    atomic_write(link, "new")
    assert link.is_symlink()
    assert real.read_text() == "new"


def test_backup_has_the_original_mode_and_content(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("secret")
    target.chmod(0o600)
    copy = backup(target)
    assert copy.read_text() == "secret"
    assert copy.stat().st_mode & 0o777 == 0o600
    assert copy.name.startswith("config.json.bak-")
