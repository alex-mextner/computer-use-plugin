"""write_pdf's Chrome under AppArmor's user-namespace restriction (#14): detection against a scratch /proc file, a scratch
/etc/apparmor.d and a scratch HOME, and the warning in `computer mcp install` and `computer claude status`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from computerlib import pdf_sandbox
from computerlib.cli import main
from tests.fakes import fake_node, fake_npm

README = Path(__file__).resolve().parent.parent / "README.md"
# What Ubuntu's tunables/home says, the variables a profile for a path under HOME uses.
UBUNTU_TUNABLES_HOME = "@{HOMEDIRS}=/home/\n@{HOME}=@{HOMEDIRS}/*/ /root/\ninclude if exists <tunables/home.d>\n"
CACHE = ".claude-server-commander/puppeteer-cache/chrome"


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A restricted host (the conftest already points the module at scratch paths) with no packaged browser, whose list of loaded
    profiles this user can read."""
    pdf_sandbox.RESTRICTION_FILE.write_text("1\n")
    pdf_sandbox.LOADED_PROFILES.write_text("")
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


def profile(name: str, text: str, loaded: bool = True) -> None:
    """Writes the profile file and, unless `loaded` is False, lists its top-level profiles the way the kernel lists loaded ones:
    `profile NAME ...` under NAME, a profile named by its path under that path."""
    (pdf_sandbox.PROFILE_DIR / name).write_text(text)
    if loaded:
        names = re.findall(r'^profile\s+"?([^"\s]+)|^(/\S+)\s.*\{$', text, re.MULTILINE)
        with pdf_sandbox.LOADED_PROFILES.open("a") as listing:
            listing.writelines(f"{named or path} (enforce)\n" for named, path in names)


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


@pytest.mark.parametrize(
    "body",
    ["  @{exec_path} mr,\n", "  profile inner /usr/bin/x {\n    userns,\n  }\n"],
    ids=["no-userns", "userns-only-in-a-child"],
)
def test_a_profile_without_userns_or_with_it_only_in_a_child_profile_does_not_cover(host: Path, body: str) -> None:
    home = host / "home"
    cached_chrome(home)
    profile("one", f"profile a {home}/{CACHE}/*/chrome-linux64/chrome flags=(unconfined) {{\n{body}}}\n")
    found = pdf_sandbox.problem(home)
    assert found is not None and "profile a in" in found.detail and "does not allow `userns,`" in found.detail


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


def test_a_profile_on_disk_the_kernel_does_not_list_is_unverified_coverage(host: Path) -> None:
    home = home_under_home_dirs(host)
    profile("desktop-commander-chrome", readme_profile(), loaded=False)
    found = pdf_sandbox.problem(home)
    assert found is not None
    assert found.detail.startswith("write_pdf's Chrome (the Chrome for Testing it downloads into ")
    assert ") is covered only on disk (unverified): AppArmor restricts unprivileged user namespaces" in found.detail
    assert f"desktop-commander-chrome in {pdf_sandbox.PROFILE_DIR / 'desktop-commander-chrome'} allows `userns,`" in found.detail
    assert "but the kernel's loaded profiles do not include it; unless it is loaded, Chrome stops with" in found.detail
    assert f"as root, load it with `apparmor_parser -r {pdf_sandbox.PROFILE_DIR / 'desktop-commander-chrome'}`" in found.fix
    pdf_sandbox.LOADED_PROFILES.write_text("snap.firefox.firefox (enforce)\ndesktop-commander-chrome (unconfined)\n")
    assert pdf_sandbox.problem(home) is None


def test_a_profile_whose_loading_cannot_be_confirmed_is_unverified_coverage(host: Path) -> None:
    # Neither of the kernel's lists can be read: the profile on disk proves nothing about what the kernel enforces.
    home = home_under_home_dirs(host)
    profile("desktop-commander-chrome", readme_profile())
    pdf_sandbox.LOADED_PROFILES.unlink()
    found = pdf_sandbox.problem(home)
    assert found is not None
    assert ") is covered only on disk (unverified): AppArmor restricts unprivileged user namespaces" in found.detail
    loaded, policy = pdf_sandbox.LOADED_PROFILES, pdf_sandbox.POLICY_PROFILES
    assert f"whether the kernel loaded it cannot be confirmed (neither {policy} nor {loaded} is readable)" in found.detail
    assert '"No usable sandbox!"' in found.detail


def test_without_root_the_per_profile_policy_directories_confirm_a_loaded_profile(host: Path) -> None:
    # As a normal user the kernel's list is EACCES; its per-profile directories (one `name` file each) stay readable.
    home = home_under_home_dirs(host)
    profile("desktop-commander-chrome", readme_profile())
    pdf_sandbox.LOADED_PROFILES.unlink()
    for name, entry in (("firefox", "firefox.3"), ("desktop-commander-chrome", "desktop-commander-chrome.41")):
        (pdf_sandbox.POLICY_PROFILES / entry).mkdir(parents=True)
        (pdf_sandbox.POLICY_PROFILES / entry / "name").write_text(f"{name}\n")
    assert pdf_sandbox.problem(home) is None
    (pdf_sandbox.POLICY_PROFILES / "desktop-commander-chrome.41" / "name").unlink()
    (pdf_sandbox.POLICY_PROFILES / "desktop-commander-chrome.41").rmdir()
    found = pdf_sandbox.problem(home)
    assert found is not None and "but the kernel's loaded profiles do not include it" in found.detail


@pytest.mark.parametrize(
    ("generic", "exact", "covered"),
    [("userns,", "deny userns,", False), ("userns,", "capability sys_admin,", False), ("deny userns,", "userns,", True)],
)
def test_the_exact_attachment_decides_over_a_generic_one(host: Path, generic: str, exact: str, covered: bool) -> None:
    home = host / "home"
    chrome = cached_chrome(home)
    profile("generic", f"profile generic {home}/** {{\n  {generic}\n}}\n")
    profile("exact", f"profile exact {chrome} {{\n  {exact}\n}}\n")
    found = pdf_sandbox.problem(home)
    assert (found is None) is covered
    if found is not None:
        assert f"exact in {pdf_sandbox.PROFILE_DIR / 'exact'}, the most specific profile attaching to it" in found.detail


def test_one_profile_in_two_files_is_one_profile_not_a_conflict(host: Path) -> None:
    # apparmor_parser loads a `.bak` copy too; the later file replaces the profile of the same name, it does not tie with it.
    home = home_under_home_dirs(host)
    profile("desktop-commander-chrome", readme_profile())
    profile("desktop-commander-chrome.bak", readme_profile())
    assert pdf_sandbox.problem(home) is None
    # Which copy the kernel holds cannot be told, so one that does not grant `userns`, or attaches elsewhere, decides.
    profile("desktop-commander-chrome.bak", readme_profile().replace("  userns,\n", ""))
    found = pdf_sandbox.problem(home)
    assert found is not None and "is defined differently in" in found.detail
    profile("desktop-commander-chrome.bak", readme_profile().replace("puppeteer-cache/chrome/*/", "old-cache/*/"))
    assert pdf_sandbox.problem(home) is not None


@pytest.mark.parametrize(
    "header",
    ["profile desktop-commander-chrome flags=(unconfined) {", "profile desktop-commander-chrome @{UNSET}/chrome {"],
    ids=["no-attachment", "attachment-expands-to-nothing"],
)
def test_a_same_named_copy_that_attaches_nowhere_makes_the_coverage_doubtful(host: Path, header: str) -> None:
    # If the kernel holds that copy, nothing attaches to Chrome.
    home = home_under_home_dirs(host)
    profile("desktop-commander-chrome", readme_profile())
    profile("desktop-commander-chrome.bak", f"{header}\n  userns,\n}}\n")
    found = pdf_sandbox.problem(home)
    assert found is not None and "is defined differently in" in found.detail


def test_a_profile_named_by_a_path_with_variables_is_confirmed_under_its_kernel_name(host: Path) -> None:
    # apparmor_parser names it with a multi-valued variable written as an alternation and keeps the doubled slash.
    home = home_under_home_dirs(host)
    header = f"@{{HOME}}/{CACHE}/*/chrome-linux64/chrome flags=(unconfined) {{\n  userns,\n}}\n"
    profile("desktop-commander-chrome", header, loaded=False)
    found = pdf_sandbox.problem(home)
    assert found is not None and "the kernel's loaded profiles do not include it" in found.detail
    pdf_sandbox.LOADED_PROFILES.write_text(f"{{/home//*/,/root/}}/{CACHE}/*/chrome-linux64/chrome (unconfined)\n")
    assert pdf_sandbox.problem(home) is None


def test_among_globs_the_longest_literal_prefix_decides_and_a_tie_attaches_neither(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    profile("broad", f"profile broad {home}/** {{\n  userns,\n}}\n")
    profile("narrow", f"profile narrow {home}/{CACHE}/*/chrome-linux64/chrome {{\n}}\n")
    assert pdf_sandbox.problem(home) is not None
    profile("narrow", f"profile narrow {home}/{CACHE}/*/chrome-linux64/chrome {{\n  userns,\n}}\n")
    assert pdf_sandbox.problem(home) is None
    # The kernel refuses two profiles that match equally specifically ("conflicting profile attachments").
    profile("twin", f"profile twin {home}/{CACHE}/*/chrome-linux64/chrom? {{\n  userns,\n}}\n")
    found = pdf_sandbox.problem(home)
    assert found is not None and "attach to it equally specifically" in found.detail


def test_a_comma_inside_a_path_glob_does_not_end_the_rule(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    chrome = f"{home}/{CACHE}/*/chrome-linux64/chrome"
    profile("globs", f"profile g {chrome} {{\n  /srv/{{a,userns,b}} r,\n}}\n")
    assert pdf_sandbox.problem(home) is not None  # `userns` is one alternative of a file path, not a rule
    builds = f"{home}/{CACHE}/{{linux-1,linux-157.0.8103.78}}/chrome-linux64/chrome"
    profile("globs", f"profile g {builds} flags=(attach_disconnected, mediate_deleted) {{\n  /srv/{{a,b}} r, userns,\n}}\n")
    assert pdf_sandbox.problem(home) is None


@pytest.mark.parametrize("brace", ["[{]", "\\{"])
def test_a_literal_brace_in_an_attachment_path_neither_hides_a_profile_nor_breaks_the_match(host: Path, brace: str) -> None:
    home = host / "ho{me"
    cached_chrome(home)
    written_home = str(home).replace("{", brace)
    profile(
        "braces",
        f"profile other /opt/odd{brace}dir/tool {{\n}}\n"
        f"profile chrome {written_home}/{CACHE}/*/chrome-linux64/chrome {{\n  userns,\n}}\n",
    )
    assert pdf_sandbox.problem(home) is None


def test_a_block_brace_right_after_the_flags_list_still_opens_the_profile(host: Path) -> None:
    home = host / "home"
    cached_chrome(home)
    chrome = f"{home}/{CACHE}/*/chrome-linux64/chrome"
    profile(
        "glued",
        f"profile other /usr/bin/other flags=(complain){{\n}}\nprofile chrome {chrome} flags=(unconfined){{\n  userns,\n}}\n",
    )
    assert pdf_sandbox.problem(home) is None


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
