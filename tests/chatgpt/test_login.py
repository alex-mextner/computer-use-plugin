"""`computer chatgpt login` (the Cloudflare building block of `install`) against the fake cloudflared, in a scratch HOME.

What is proven here: cloudflared's state lives in this target's own directory (the scratch ~/.cloudflared is never touched), the
login runs under a staging HOME with an empty PATH and its URL is opened by us, re-running changes nothing, a new DNS record is
created without a question but an existing one that points elsewhere is only replaced with consent, and lost credentials are
re-fetched.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from computerlib.targets.chatgpt import cloudflare
from computerlib.targets.chatgpt.settings import Settings, load_settings, save_settings
from tests.chatgpt import fake_cloudflared
from tests.chatgpt.cli_helpers import layout, run_cli
from tests.chatgpt.fake_cloudflared import FAKE_CLOUDFLARED

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
    assert not (own / "login-home").exists()
    assert fake_cloudflared.tunnels(account) == ["my-tunnel"]
    assert fake_cloudflared.routes(account) == {HOST: "my-tunnel"}
    assert opened == ["https://dash.cloudflare.com/argotunnel?aud=&callback=https%3A%2F%2Flogin.cloudflareaccess.org%2Ffake"]
    assert "0000000000aa" not in result.out + result.err  # the tunnel id is never printed


def test_dot_cloudflared_is_never_used_and_the_login_cannot_start_a_browser_itself(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    dot = Path(os.environ["HOME"]) / ".cloudflared"
    run_cli(capsys, "login")
    assert not dot.exists()
    own = layout().cloudflared_dir
    for call in fake_cloudflared.calls(account):
        if call.argv[:2] == ["tunnel", "login"]:
            assert call.home == str(own / "login-home") and call.path == ""
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
    assert result.code == 0
    assert "kept the existing record" in result.out
    assert fake_cloudflared.routes(account) == {HOST: "someone-elses-tunnel"}
    assert not any("--overwrite-dns" in call for call in _calls(account))


def test_yes_replaces_an_existing_record(capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]) -> None:
    fake_cloudflared.existing_record(account, HOST, "someone-elses-tunnel")
    result = run_cli(capsys, "login", "--yes")
    assert result.code == 0, result.err
    assert "replaced" in result.out
    assert fake_cloudflared.routes(account) == {HOST: "my-tunnel"}


def test_skip_dns_never_touches_the_zone(capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]) -> None:
    run_cli(capsys, "login", "--skip-dns")
    assert not any(" route " in call for call in _calls(account))
    assert fake_cloudflared.routes(account) == {}


def test_lost_credentials_of_an_existing_tunnel_are_fetched_not_recreated(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    run_cli(capsys, "login")
    credentials = layout().cloudflared_dir / "my-tunnel.json"
    credentials.chmod(0o600)
    credentials.unlink()
    result = run_cli(capsys, "login")
    assert result.code == 0, result.err
    assert credentials.is_file() and "credentials fetched" in result.out
    assert fake_cloudflared.tunnels(account) == ["my-tunnel"]


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
    assert "computer chatgpt install --hostname" in result.err
