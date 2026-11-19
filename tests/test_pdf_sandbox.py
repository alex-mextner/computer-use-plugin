"""write_pdf's Chrome under AppArmor's user-namespace restriction (#14): detection against a scratch /proc file, a scratch
/etc/apparmor.d and a scratch HOME, and the warning in `computer mcp install`, `computer chatgpt doctor` and
`computer claude status`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from computerlib import pdf_sandbox
from computerlib.cli import main
from computerlib.targets.chatgpt.commands import doctor
from tests.fakes import fake_node, fake_npm

README = Path(__file__).resolve().parent.parent / "README.md"
# What Ubuntu's tunables/home says, the variables a profile for a path under HOME uses.
UBUNTU_TUNABLES_HOME = "@{HOMEDIRS}=/home/\n@{HOME}=@{HOMEDIRS}/*/ /root/\ninclude if exists <tunables/home.d>\n"
CACHE = ".claude-server-commander/puppeteer-cache/chrome"


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A restricted host (the conftest already points the module at scratch paths) with no packaged browser."""
    pdf_sandbox.RESTRICTION_FILE.write_text("1\n")
    (pdf_sandbox.PROFILE_DIR / "tunables").mkdir(parents=True)
    (pdf_sandbox.PROFILE_DIR / "tunables" / "home").write_text(UBUNTU_TUNABLES_HOME)
    monkeypatch.setattr(pdf_sandbox, "SYSTEM_CHROMES", (str(tmp_path / "usr/bin/google-chrome"),))
    return tmp_path


def home_under_home_dirs(tmp_path: Path) -> Path:
    """@{HOME} only matches /home/<user>/: the check is given a home of that shape that never exists on disk."""
    return Path("/home") / f"no-such-user-{tmp_path.name}"


def cached_chrome(home: Path, build: str = "linux-157.0.8103.78") -> Path:
    chrome = home / CACHE / build / "chrome-linux64" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("")
    return chrome


def profile(name: str, text: str) -> None:
    (pdf_sandbox.PROFILE_DIR / name).write_text(text)


def readme_profile() -> str:
    section = README.read_text().split("### PDF output on Ubuntu 23.10+ (AppArmor)", 1)[1]
    block = re.search(r"```\n(.*?)```", section, re.DOTALL)
    assert block is not None
    return "\n".join(line.removeprefix("   ") for line in block[1].splitlines())


def test_no_restriction_file_or_a_lifted_restriction_is_no_problem(tmp_path: Path) -> None:
    assert pdf_sandbox.problem(tmp_path / "home") is None
    pdf_sandbox.RESTRICTION_FILE.write_text("0\n")
    assert pdf_sandbox.problem(tmp_path / "home") is None


def test_a_chrome_still_to_download_without_a_profile_is_named_with_both_fixes(host: Path) -> None:
    home = host / "home"
    found = pdf_sandbox.problem(home)
    assert found is not None
    assert f"(the Chrome for Testing it downloads into {home / CACHE})" in found.detail and "No usable sandbox!" in found.detail
    assert "kernel.apparmor_restrict_unprivileged_userns=1" in found.detail
    # Both documented options: a `userns,` profile for the downloaded build (any build), or the sysctl.
    assert f"`userns,` for {home / CACHE}/*/chrome-linux64/chrome" in found.fix and "apparmor_parser -r" in found.fix
    assert "sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" in found.fix
    assert pdf_sandbox.DOCS_URL in found.fix


def test_the_readme_profile_covers_the_cached_build_and_the_one_still_to_download(host: Path) -> None:
    home = home_under_home_dirs(host)
    profile("desktop-commander-chrome", readme_profile())
    assert pdf_sandbox.problem(home) is None


def test_the_readme_profile_covers_a_real_cached_build(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    # @{HOME} as tunables/home would say for this home, wherever the test's scratch directory is.
    (pdf_sandbox.PROFILE_DIR / "tunables" / "home").write_text(f"@{{HOME}}={home}/\n")
    profile("desktop-commander-chrome", readme_profile())
    assert pdf_sandbox.problem(home) is None


def test_a_profile_without_userns_or_with_it_only_in_a_child_profile_does_not_cover(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    path = f"{home}/{CACHE}/*/chrome-linux64/chrome"
    profile("no-userns", f"profile a {path} flags=(unconfined) {{\n  @{{exec_path}} mr,\n}}\n")
    profile("child", f"profile b {path} {{\n  profile inner /usr/bin/x {{\n    userns,\n  }}\n}}\n")
    assert pdf_sandbox.problem(home) is not None


def test_a_disabled_profile_does_not_cover(host: Path) -> None:
    home = home_under_home_dirs(host)
    profile("desktop-commander-chrome", readme_profile())
    (pdf_sandbox.PROFILE_DIR / "disable").mkdir()
    (pdf_sandbox.PROFILE_DIR / "disable" / "desktop-commander-chrome").write_text("")
    assert pdf_sandbox.problem(home) is not None


def test_a_backup_of_a_profile_does_not_cover(host: Path) -> None:
    # apparmor_parser skips editor and package-manager leftovers when it loads the directory.
    home = home_under_home_dirs(host)
    for name in ("desktop-commander-chrome~", "desktop-commander-chrome.dpkg-old", ".desktop-commander-chrome"):
        profile(name, readme_profile())
    assert pdf_sandbox.problem(home) is not None


@pytest.mark.parametrize("rules", [["userns,", "deny userns,"], ["deny userns,", "userns,"]])
def test_a_deny_rule_wins_over_the_grant_in_either_order(host: Path, rules: list[str]) -> None:
    home = host / "home"
    cached_chrome(home)
    profile("denied", f"profile d {home}/{CACHE}/*/chrome-linux64/chrome {{\n" + "\n".join(rules) + "\n}\n")
    assert pdf_sandbox.problem(home) is not None


def test_a_grant_or_deny_from_an_included_local_override_counts(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    local = pdf_sandbox.PROFILE_DIR / "local"
    local.mkdir()
    profile(
        "included",
        f"profile i {home}/{CACHE}/*/chrome-linux64/chrome {{\n  include if exists <local/included>\n}}\n",
    )
    assert pdf_sandbox.problem(home) is not None
    (local / "included").write_text("# site override\nuserns,\n")
    assert pdf_sandbox.problem(home) is None
    (local / "included").write_text("userns,\n#include <local/deny>\n")
    (local / "deny").write_text("deny userns,\n")
    assert pdf_sandbox.problem(home) is not None


def test_an_unreadable_included_directory_is_no_grant_and_never_raises(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    site = pdf_sandbox.PROFILE_DIR / "site.d"
    site.mkdir()
    (site / "grant").write_text("userns,\n")
    profile("via-dir", f"profile v {home}/{CACHE}/*/chrome-linux64/chrome {{\n  include <site.d>\n}}\n")
    assert pdf_sandbox.problem(home) is None
    site.chmod(0)
    try:
        assert pdf_sandbox.problem(home) is not None  # rules this user cannot read are not a grant
    finally:
        site.chmod(0o755)


def test_a_deny_on_the_same_line_as_the_grant_wins(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    profile("one-line", f"profile l {home}/{CACHE}/*/chrome-linux64/chrome {{\n  userns, deny userns,\n}}\n")
    assert pdf_sandbox.problem(home) is not None
    profile("one-line", f"profile l {home}/{CACHE}/*/chrome-linux64/chrome {{\n  capability sys_admin, userns,\n}}\n")
    assert pdf_sandbox.problem(home) is None


def test_a_child_profile_is_not_a_global_attachment(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    chrome = f"{home}/{CACHE}/*/chrome-linux64/chrome"
    profile("nested", f"profile other /usr/bin/other {{\n  profile chrome {chrome} {{\n    userns,\n  }}\n}}\n")
    assert pdf_sandbox.problem(home) is not None


def test_only_the_build_desktop_commander_launches_is_checked(host: Path) -> None:
    # It takes the highest build in numeric order (157 > 99), so a profile for that one alone is enough.
    home = host / "home"
    cached_chrome(home, "linux-99.0.1.2")
    launched = cached_chrome(home, "linux-157.0.8103.78")
    profile("one-build", f"profile o {launched} {{\n  userns,\n}}\n")
    assert pdf_sandbox.problem(home) is None


def test_variables_alternations_and_globs_of_a_profile_are_followed(host: Path) -> None:
    home = host / "home"
    cached_chrome(home, "linux-2")
    profile(
        "alternation",
        f"@{{builds}} = linux-1 linux-2  # the two builds\n"
        f'profile c "{home}/{CACHE}/@{{builds}}/{{chrome-linux64,chrome-linux}}/chr?me" {{\n  allow userns,\n}}\n',
    )
    assert pdf_sandbox.problem(home) is None


def test_a_star_does_not_cross_a_directory(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    profile("shallow", f"{home}/{CACHE}/*/chrome {{\n  userns,\n}}\n")
    assert pdf_sandbox.problem(home) is not None
    profile("deep", f"{home}/{CACHE}/**/chrome {{\n  userns,\n}}\n")
    assert pdf_sandbox.problem(home) is None


def test_a_packaged_browser_is_trusted_but_only_when_the_cache_is_empty(host: Path) -> None:
    home = host / "home"
    packaged = Path(pdf_sandbox.SYSTEM_CHROMES[0])
    packaged.parent.mkdir(parents=True)
    packaged.write_text("")
    assert pdf_sandbox.problem(home) is None
    # Desktop Commander launches its cached build first, so the packaged browser does not help then.
    chrome = cached_chrome(home)
    found = pdf_sandbox.problem(home)
    assert found is not None and str(chrome) in found.detail


def test_mcp_install_warns_with_the_fix(host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    bin_dir = host / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    assert main(["mcp", "install"]) == 0
    first = capsys.readouterr().out
    assert "\nwarning: write_pdf cannot start Chrome" in first and "; fix: as root, add an AppArmor profile" in first
    # Also when the install is already there: the restriction and the profiles are not part of the install.
    assert main(["mcp", "install"]) == 0
    assert "already installed" in (again := capsys.readouterr().out) and "warning: write_pdf cannot start Chrome" in again


def test_doctor_warns_without_failing(host: Path) -> None:
    check = doctor._pdf_sandbox()  # pyright: ignore[reportPrivateUsage]
    assert (check.name, check.ok, check.required) == ("PDF browser sandbox (write_pdf)", False, False)
    assert "sysctl -w" in check.fix
    pdf_sandbox.RESTRICTION_FILE.write_text("0\n")
    assert doctor._pdf_sandbox().ok  # pyright: ignore[reportPrivateUsage]


def test_doctor_prints_a_failed_optional_check_as_a_warning_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checks = [doctor.Check("needed", True, "fine"), doctor.Check("optional", False, "broken", "do this", required=False)]

    def collect(_ctx: object, _local_only: bool) -> list[doctor.Check]:
        return checks

    monkeypatch.setattr(doctor, "load_context", lambda: None)
    monkeypatch.setattr(doctor, "collect", collect)
    assert main(["chatgpt", "doctor", "--local"]) == 0
    out = capsys.readouterr().out
    assert "[warn] optional: broken\n       fix: do this" in out
    checks.append(doctor.Check("needed too", False, "down", required=True))
    assert main(["chatgpt", "doctor", "--local"]) == 3
