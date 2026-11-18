"""`computer chatgpt login` (the Cloudflare building block of `install`) against the fake cloudflared, in a scratch HOME.

What is proven here: cloudflared's state lives in this target's own directory (the scratch ~/.cloudflared is never touched), the
login runs under a staging HOME with an empty PATH and its URL is shown on its own line and opened by us (or left to another
device on a headless machine), re-running changes nothing, a new DNS record is created without a question but an existing one that
points elsewhere is only replaced with consent, an existing tunnel without local credentials is refused unless named with
--tunnel-name, and an account certificate from another machine can stand in for the browser login.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from agenttools_errors import ConfigError

from computerlib.targets.chatgpt import cloudflare
from computerlib.targets.chatgpt.settings import Settings, load_settings, save_settings
from tests.chatgpt import fake_cloudflared
from tests.chatgpt.cli_helpers import layout, run_cli
from tests.chatgpt.fake_cloudflared import FAKE_CLOUDFLARED, account_cert

HOST = "dc.example.test"


@pytest.fixture
def account(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The fake Cloudflare account; returns its state directory. The installed cloudflared is the fake."""
    state = tmp_path / "cloudflare-account"
    monkeypatch.setenv("FAKE_CF_STATE", str(state))
    save_settings(Settings(hostname=HOST, tunnel_name="my-tunnel"))
    binary = layout().bin_dir / "cloudflared"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(FAKE_CLOUDFLARED)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return state


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []

    def fake_open(url: str) -> bool:
        urls.append(url)
        return True

    monkeypatch.setattr(cloudflare, "open_in_browser", fake_open)
    return urls


def _calls(state: Path) -> list[str]:
    return [" ".join(call.argv) for call in fake_cloudflared.calls(state)]


def test_login_logs_in_creates_the_tunnel_and_routes_the_hostname(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    result = run_cli(capsys, "login")
    assert result.code == 0, result.err
    own = layout().cloudflared_dir
    assert (own / "cert.pem").read_text() == "fake account certificate\n"
    assert stat.S_IMODE((own / "cert.pem").stat().st_mode) == 0o600
    assert stat.S_IMODE(own.stat().st_mode) == 0o700
    assert (own / "my-tunnel.json").is_file() and (own / "config.yml").is_file()
    assert not list(own.glob("login-home*"))
    assert fake_cloudflared.tunnels(account) == ["my-tunnel"]
    assert fake_cloudflared.routes(account) == {HOST: "my-tunnel"}
    assert opened == ["https://dash.cloudflare.com/argotunnel?aud=&callback=https%3A%2F%2Flogin.cloudflareaccess.org%2Ffake"]
    printed = result.out + result.err
    # Neither the tunnel id nor the tunnel secret of the credentials file nor the account certificate is ever printed.
    assert not any(secret in printed for secret in ("0000000000aa", "c2VjcmV0", "fake account certificate"))


def test_dot_cloudflared_is_never_used_and_the_login_cannot_start_a_browser_itself(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    dot = Path(os.environ["HOME"]) / ".cloudflared"
    run_cli(capsys, "login")
    assert not dot.exists()
    own = layout().cloudflared_dir
    for call in fake_cloudflared.calls(account):
        if call.argv[:2] == ["tunnel", "login"]:
            home = Path(call.home)
            assert home.parent == own and home.name.startswith("login-home-") and call.path == ""
        else:
            assert call.argv[1:5] == ["--config", str(own / "config.yml"), "--origincert", str(own / "cert.pem")]


def test_running_login_again_changes_nothing(capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]) -> None:
    run_cli(capsys, "login")
    before = _calls(account)
    again = run_cli(capsys, "login")
    assert again.code == 0, again.err
    new = _calls(account)[len(before) :]
    assert not any(" login" in call or " create " in call or "--overwrite-dns" in call for call in new)
    assert "logged in" in again.out and "tunnel my-tunnel: exists" in again.out and "already routed" in again.out


def test_an_existing_record_that_points_elsewhere_is_kept_without_consent(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    fake_cloudflared.existing_record(account, HOST, "someone-elses-tunnel")
    result = run_cli(capsys, "login")
    assert result.code == ConfigError.exit_code
    assert "kept the existing record" in result.out
    assert f"{HOST} still points somewhere else" in result.err and "--yes" in result.err
    assert fake_cloudflared.routes(account) == {HOST: "someone-elses-tunnel"}
    assert not any("--overwrite-dns" in call for call in _calls(account))


def test_yes_replaces_an_existing_record(capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]) -> None:
    fake_cloudflared.existing_record(account, HOST, "someone-elses-tunnel")
    result = run_cli(capsys, "login", "--yes")
    assert result.code == 0, result.err
    assert "replaced" in result.out
    assert fake_cloudflared.routes(account) == {HOST: "my-tunnel"}


def test_skip_dns_never_touches_the_zone(capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]) -> None:
    fake_cloudflared.existing_record(account, HOST, "someone-elses-tunnel")
    result = run_cli(capsys, "login", "--skip-dns")
    assert result.code == 0, result.err
    assert not any(" route " in call for call in _calls(account))
    assert fake_cloudflared.routes(account) == {HOST: "someone-elses-tunnel"}


def test_lost_credentials_of_an_existing_tunnel_are_refused_unless_the_tunnel_is_named(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    run_cli(capsys, "login")
    credentials = layout().cloudflared_dir / "my-tunnel.json"
    credentials.chmod(0o600)
    credentials.unlink()
    refused = run_cli(capsys, "login")
    assert refused.code == 2
    assert "no credentials for it" in refused.err and "--tunnel-name my-tunnel" in refused.err
    assert not credentials.exists()
    adopted = run_cli(capsys, "login", "--tunnel-name", "my-tunnel")
    assert adopted.code == 0, adopted.err
    assert credentials.is_file() and "adopted" in adopted.out
    assert fake_cloudflared.tunnels(account) == ["my-tunnel"]


def test_an_empty_tunnel_name_is_refused_and_never_adopts_the_saved_one(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """`--tunnel-name "$UNSET"` must not fall back to the saved name with the adoption an explicit name grants."""
    run_cli(capsys, "login")
    credentials = layout().cloudflared_dir / "my-tunnel.json"
    credentials.chmod(0o600)
    credentials.unlink()
    routes_before = fake_cloudflared.routes(account)
    calls_before = len(fake_cloudflared.calls(account))
    refused = run_cli(capsys, "login", "--tunnel-name", "")
    assert refused.code == 2 and "cannot name a tunnel" in refused.err
    assert not credentials.exists()
    assert fake_cloudflared.calls(account)[calls_before:] == []
    assert fake_cloudflared.routes(account) == routes_before


def test_a_uuid_is_refused_as_a_tunnel_name_before_cloudflared_runs(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    refused = run_cli(capsys, "login", "--tunnel-name", "550e8400-e29b-41d4-a716-446655440000")
    assert refused.code == 2 and "read as a UUID" in refused.err
    assert fake_cloudflared.calls(account) == []


def test_a_headless_login_shows_the_url_to_open_elsewhere_and_waits(
    capsys: pytest.CaptureFixture[str], account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert cloudflare.open_in_browser("https://example.invalid/") is False  # no display: never tries to start a browser
    result = run_cli(capsys, "login")
    assert result.code == 0, result.err
    url = "https://dash.cloudflare.com/argotunnel?aud=&callback=https%3A%2F%2Flogin.cloudflareaccess.org%2Ffake"
    assert f"\n  {url}\n" in result.out
    assert "open this URL in a browser on any device" in result.out and "no browser on this machine" in result.out


def test_an_account_certificate_from_another_machine_skips_the_browser(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], tmp_path: Path
) -> None:
    cert = tmp_path / "cert.pem"
    cert.write_text(account_cert("a"))
    result = run_cli(capsys, "login", "--cloudflare-cert", str(cert))
    assert result.code == 0, result.err
    own = layout().cloudflared_dir / "cert.pem"
    assert own.read_text() == cert.read_text() and stat.S_IMODE(own.stat().st_mode) == 0o600
    assert opened == [] and not any(c.argv[:2] == ["tunnel", "login"] for c in fake_cloudflared.calls(account))
    assert "copied from" in result.out
    assert "already the same" in run_cli(capsys, "login", "--cloudflare-cert", str(cert)).out
    other = tmp_path / "other.pem"
    other.write_text(account_cert("b"))
    refused = run_cli(capsys, "login", "--cloudflare-cert", str(other))
    assert refused.code == 2 and "different Cloudflare account certificate" in refused.err
    assert own.read_text() == cert.read_text()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not a certificate\n",
        "-----BEGIN ARGO TUNNEL TOKEN-----\nbm90IGpzb24=\n-----END ARGO TUNNEL TOKEN-----\n",
        account_cert("a") + account_cert("b"),  # cloudflared refuses more than one token
        " ".join(account_cert("a").split()) + "\n",  # `echo $(ssh … cat cert.pem)`: pem.Decode finds no block on one line
        account_cert("a") + "-----BEGIN SOMETHING ELSE-----\nAAAA\n-----END SOMETHING ELSE-----\n",  # "unknown block"
        account_cert("a") + "-----BEGIN SOMETHING-ELSE-----\nAAAA\n-----END SOMETHING-ELSE-----\n",  # hyphen in the type
        account_cert("a") + account_cert("b").replace("-----\n", "-----  \n"),  # pem.Decode allows trailing blanks
        "-----BEGIN ARGO TUNNEL TOKEN-----\n" + "\n" * 5000,  # no END: must fail fast, not backtrack
        "-----BEGIN ARGO TUNNEL TOKEN-----\n"
        + base64.b64encode(json.dumps({"zoneID": "z", "accountID": "a", "apiToken": "t", "endpoint": 42}).encode()).decode()
        + "\n-----END ARGO TUNNEL TOKEN-----\n",  # encoding/json refuses a number for the string endpoint
    ],
    ids=[
        "empty",
        "not-pem",
        "not-a-token",
        "two-tokens",
        "one-line",
        "unknown-block",
        "hyphenated-block",
        "second-token-trailing-blanks",
        "unterminated",
        "numeric-endpoint",
    ],
)
def test_a_file_that_is_not_an_account_certificate_is_refused_and_leaves_login_possible(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], tmp_path: Path, content: str
) -> None:
    """Publishing a bad file would make every later run think it is logged in and refuse the right certificate as different."""
    bad = tmp_path / "bad.pem"
    bad.write_text(content)
    refused = run_cli(capsys, "login", "--cloudflare-cert", str(bad))
    assert refused.code == 2 and "is not a Cloudflare account certificate" in refused.err
    assert not (layout().cloudflared_dir / "cert.pem").exists()
    good = tmp_path / "cert.pem"
    good.write_text(account_cert("a"))
    imported = run_cli(capsys, "login", "--cloudflare-cert", str(good))
    assert imported.code == 0, imported.err
    assert (layout().cloudflared_dir / "cert.pem").read_text() == good.read_text()


def test_a_browser_login_still_works_after_a_refused_import(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.pem"
    bad.write_text("")
    assert run_cli(capsys, "login", "--cloudflare-cert", str(bad)).code == 2
    result = run_cli(capsys, "login")
    assert result.code == 0, result.err
    assert any(c.argv[:2] == ["tunnel", "login"] for c in fake_cloudflared.calls(account))


def test_a_filesystem_without_hard_links_still_publishes_and_never_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vfat, exFAT and some FUSE mounts refuse link(2) with EPERM: a complete copy is then renamed into place under a lock."""

    def no_links(source: object, target: object) -> None:
        del source, target
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(cloudflare.os, "link", no_links)
    target = cloudflare.Cloudflare(tmp_path / "cloudflared", tmp_path / "own")
    first, other = tmp_path / "a.pem", tmp_path / "b.pem"
    first.write_text(account_cert("a"))
    other.write_text(account_cert("b"))
    assert target.import_cert(cloudflare.read_account_cert(first))
    assert target.cert.read_text() == account_cert("a") and stat.S_IMODE(target.cert.stat().st_mode) == 0o600
    assert not target.import_cert(cloudflare.read_account_cert(first))  # the same one again: nothing to do
    with pytest.raises(ConfigError):
        target.import_cert(cloudflare.read_account_cert(other))
    assert target.cert.read_text() == account_cert("a")
    assert sorted(p.name for p in target.directory.iterdir()) == [".cert.lock", "cert.pem", "config.yml"]


def test_a_failed_copy_leaves_no_partial_certificate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial cert.pem would read as "logged in" forever and refuse the right certificate as a different one."""

    def no_links(source: object, target: object) -> None:
        del source, target
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(cloudflare.os, "link", no_links)
    target = cloudflare.Cloudflare(tmp_path / "cloudflared", tmp_path / "own")
    source = tmp_path / "a.pem"
    source.write_text(account_cert("a"))
    real_fdopen = os.fdopen

    def failing_fdopen(fd: int, mode: str = "r", encoding: str | None = None, errors: str | None = None) -> object:
        if mode == "wb":
            os.close(fd)
            raise OSError(28, "No space left on device")
        return real_fdopen(fd, mode, encoding=encoding, errors=errors)

    monkeypatch.setattr(cloudflare.os, "fdopen", failing_fdopen)
    with pytest.raises(OSError):
        target.import_cert(cloudflare.read_account_cert(source))
    assert not target.cert.exists()


@pytest.mark.parametrize("hard_links", [True, False], ids=["link", "no-hard-links"])
def test_concurrent_imports_of_different_certificates_never_replace_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard_links: bool
) -> None:
    """Two installs started at once with different accounts: exactly one certificate is published, the other is refused; also
    on a filesystem without hard links, where a reader must never see a partial file."""
    if not hard_links:

        def no_links(source: object, target: object) -> None:
            del source, target
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(cloudflare.os, "link", no_links)
    target = cloudflare.Cloudflare(tmp_path / "cloudflared", tmp_path / "own")
    sources: list[Path] = []
    for index in range(8):
        source = tmp_path / f"cert-{index}.pem"
        source.write_text(account_cert(str(index)))
        sources.append(source)
    start = threading.Barrier(len(sources))
    outcomes: dict[int, str] = {}

    def import_one(index: int) -> None:
        start.wait()
        try:
            outcomes[index] = "published" if target.import_cert(cloudflare.read_account_cert(sources[index])) else "same"
        except ConfigError:
            outcomes[index] = "refused"

    threads = [threading.Thread(target=import_one, args=(index,)) for index in range(len(sources))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [index for index, outcome in outcomes.items() if outcome == "published"]
    assert len(winners) == 1 and list(outcomes.values()).count("refused") == len(sources) - 1
    assert target.cert.read_text() == account_cert(str(winners[0]))
    assert stat.S_IMODE(target.cert.stat().st_mode) == 0o600
    left = sorted(p.name for p in target.directory.iterdir() if p.name != ".cert.lock")
    assert left == ["cert.pem", "config.yml"]  # no temp file left behind


def test_a_browser_login_never_replaces_a_certificate_imported_while_it_waited(tmp_path: Path) -> None:
    """Install A waits for the browser; install B imports account B's certificate meanwhile; A's late certificate is refused."""
    other = tmp_path / "account-b.pem"
    other.write_text(account_cert("B"))

    def login_runner(argv: list[str], env: dict[str, str], on_line: Callable[[str], None]) -> int:
        del argv, on_line
        assert target.import_cert(cloudflare.read_account_cert(other))  # the other install finishes first
        staged = Path(env["HOME"]) / ".cloudflared" / "cert.pem"
        staged.parent.mkdir(parents=True)
        staged.write_text("account A\n")
        return 0

    target = cloudflare.Cloudflare(tmp_path / "cloudflared", tmp_path / "own", login_runner=login_runner, opener=bool)
    with pytest.raises(ConfigError):
        target.login(out=lambda line: None)
    assert target.cert.read_text() == account_cert("B")


def test_concurrent_browser_logins_to_different_accounts_publish_one_certificate(tmp_path: Path) -> None:
    """Each login has its own staging HOME: the second cloudflared writing its certificate cannot alter the published one."""
    both_waiting = threading.Barrier(2)
    first_published = threading.Event()

    def login_runner(argv: list[str], env: dict[str, str], on_line: Callable[[str], None]) -> int:
        del argv, on_line
        staged = Path(env["HOME"]) / ".cloudflared" / "cert.pem"
        staged.parent.mkdir(parents=True, exist_ok=True)
        index = both_waiting.wait()  # 0 or 1: who is which account
        if index == 1:
            first_published.wait(timeout=10)  # the other login has published its certificate by now
        staged.write_text(f"account {index}\n")
        return 0

    target = cloudflare.Cloudflare(tmp_path / "cloudflared", tmp_path / "own", login_runner=login_runner, opener=bool)
    outcomes: list[str] = []

    def login() -> None:
        try:
            target.login(out=lambda line: None)
            outcomes.append("published")
            first_published.set()
        except ConfigError:
            outcomes.append("refused")

    threads = [threading.Thread(target=login) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["published", "refused"]
    assert target.cert.read_text() == "account 0\n"
    assert not list(target.directory.glob("login-home*"))


def test_an_unfinished_browser_login_is_reported_with_exit_8(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CF_LOGIN", "fail")
    result = run_cli(capsys, "login")
    assert result.code == 8
    assert "login did not finish" in result.err
    assert not (layout().cloudflared_dir / "cert.pem").exists()


def test_a_new_tunnel_name_is_saved_to_the_config(capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]) -> None:
    run_cli(capsys, "login", "--tunnel-name", "other")
    assert load_settings().tunnel_name == "other"
    assert fake_cloudflared.routes(account) == {HOST: "other"}


def test_without_the_pinned_binary_login_says_how_to_get_it(capsys: pytest.CaptureFixture[str]) -> None:
    save_settings(Settings(hostname=HOST))
    result = run_cli(capsys, "login")
    assert result.code == 5
    assert "computer chatgpt install --domain" in result.err
