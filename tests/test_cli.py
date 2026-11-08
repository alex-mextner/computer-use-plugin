from __future__ import annotations

import pytest

from computerlib import package_version
from computerlib.cli import main


def test_version_flag_prints_the_distribution_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"computer {package_version()}"


def test_bare_invocation_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "usage: computer" in out
    assert "exit codes:" in out
