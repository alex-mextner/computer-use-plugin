"""`computer chatgpt login` (the Cloudflare building block of `install`) against the fake cloudflared, in a scratch HOME.

What is proven here: cloudflared's state lives in this target's own directory (the scratch ~/.cloudflared is never touched), the
login runs under a staging HOME with an empty PATH and its URL is shown on its own line and opened by us (or left to another
device on a headless machine), re-running changes nothing, a new DNS record is created without a question but an existing one that
points elsewhere is only replaced with consent, an existing tunnel without local credentials is refused unless named with
--tunnel-name, and an account certificate from another machine can stand in for the browser login. The same-name guards (#31):
a running tunnel is adopted only with --force-takeover, the record of a --domain hostname is never replaced by --yes, and a
credentials file of another tunnel id is not this machine's tunnel; this machine's own running tunnel needs no override. And
(#46): a connector without connection data is unknown liveness, DNS is routed by the checked tunnel id whatever credentials-file
the config names, and an old credentials file without TunnelID is bound by its `<tunnel id>.json` name and migrated.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
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
OTHER_ID = "00000000-0000-0000-0000-0000000000bb"
LEGACY = {"AccountTag": "acct", "TunnelSecret": "c2VjcmV0"}  # what cloudflared wrote before credentials carried a TunnelID


def _write_legacy(path: Path, secret: str = "c2VjcmV0") -> None:
    if path.exists():
        path.chmod(0o600)
    path.write_text(json.dumps({**LEGACY, "TunnelSecret": secret}))


def _tunnel_id_in(path: Path) -> str:
    return json.loads(path.read_text())["TunnelID"]


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
    # The command to run by hand names this target's binary, config and certificate and routes by id: a bare `cloudflared`
    # would read ~/.cloudflared/config.yml, whose credentials-file may resolve the name to another tunnel.
    own = layout().cloudflared_dir
    hint = f"{layout().bin_dir / 'cloudflared'} tunnel --config {own / 'config.yml'} --origincert {own / 'cert.pem'} route dns"
    assert f'{hint} "${{COMPUTER_TUNNEL_ID:?set COMPUTER_TUNNEL_ID to the tunnel id}}" {HOST}' in result.out
    assert "0000000000aa" not in result.out


def test_the_route_command_skip_dns_prints_never_reaches_a_tunnel_by_name(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], tmp_path: Path
) -> None:
    """Pasted without an id, the printed command must not run: cloudflared would take any non-UUID word for a name and resolve
    it through the config's credentials-file (here another tunnel's); a leftover generic TUNNEL_ID does not count. With
    COMPUTER_TUNNEL_ID set it routes to that id."""
    fake_cloudflared.recreated(account, "other", OTHER_ID)
    other_credentials = tmp_path / "other.json"
    other_credentials.write_text(json.dumps({**LEGACY, "TunnelID": OTHER_ID}))
    own = layout().cloudflared_dir
    own.mkdir(parents=True, mode=0o700)
    (own / "config.yml").write_text(f"no-autoupdate: true\ncredentials-file: {other_credentials}\n")
    result = run_cli(capsys, "login", "--skip-dns")
    assert result.code == 0, result.err
    command = next(line for line in result.out.splitlines() if line.startswith("dns: skipped")).split(" then run: ", 1)[1]
    env = {key: value for key, value in os.environ.items() if key not in ("COMPUTER_TUNNEL_ID", "TUNNEL_ID")}
    for unset in (env, {**env, "COMPUTER_TUNNEL_ID": ""}, {**env, "TUNNEL_ID": OTHER_ID}):
        pasted = subprocess.run(["bash", "-c", command], env=unset, capture_output=True, text=True, timeout=60, check=False)
        assert pasted.returncode != 0 and "set COMPUTER_TUNNEL_ID to the tunnel id" in pasted.stderr
    assert not any(" route " in call for call in _calls(account))
    assert fake_cloudflared.routes(account) == {}
    tunnel_id = _tunnel_id_in(own / "my-tunnel.json")
    routed = subprocess.run(
        ["bash", "-c", command],
        env={**env, "COMPUTER_TUNNEL_ID": tunnel_id},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert routed.returncode == 0, routed.stderr
    assert fake_cloudflared.routes(account) == {HOST: "my-tunnel"}


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


def _drop_credentials() -> Path:
    credentials = layout().cloudflared_dir / "my-tunnel.json"
    credentials.chmod(0o600)
    credentials.unlink()
    return credentials


def test_adopting_a_running_tunnel_by_name_needs_force_takeover(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """Machine B, same name as A, has no credentials; `tunnel info` shows A's live connections: --tunnel-name alone (or with
    --yes) must not fetch A's credentials. Recently closed connections (pending reconnect) do not count."""
    run_cli(capsys, "login")
    credentials = _drop_credentials()
    fake_cloudflared.connected(account, "my-tunnel", 2, pending=3)
    for extra in ((), ("--yes",)):
        refused = run_cli(capsys, "login", "--tunnel-name", "my-tunnel", *extra)
        assert refused.code == 2
        assert "REFUSED" in refused.err and "RUNNING right now (2 live connection(s))" in refused.err
        assert "--force-takeover" in refused.err
        assert not credentials.exists()
        assert not any(" token " in call for call in _calls(account))
    taken = run_cli(capsys, "login", "--tunnel-name", "my-tunnel", "--force-takeover")
    assert taken.code == 0, taken.err
    assert "WARNING: --force-takeover" in taken.out and "adopted" in taken.out and credentials.is_file()
    # bound to the id that was checked, never re-resolved by name
    assert [c.split()[-1] for c in _calls(account) if " token " in c] == ["00000000-0000-0000-0000-0000000000aa"]


def test_a_tunnel_with_only_recently_closed_connections_is_adopted_by_name(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """A stopped tunnel whose files were lost: its connections are all pending reconnect, so nothing runs it."""
    run_cli(capsys, "login")
    credentials = _drop_credentials()
    fake_cloudflared.connected(account, "my-tunnel", 0, pending=2)
    adopted = run_cli(capsys, "login", "--tunnel-name", "my-tunnel")
    assert adopted.code == 0, adopted.err
    assert "adopted" in adopted.out and "WARNING" not in adopted.out and credentials.is_file()


def test_unknown_liveness_is_refused_without_force_takeover(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """Fail closed: an answer that does not say whether the tunnel runs never lets --tunnel-name adopt it."""
    run_cli(capsys, "login")
    credentials = _drop_credentials()
    fake_cloudflared.connections_unreported(account, "my-tunnel")
    refused = run_cli(capsys, "login", "--tunnel-name", "my-tunnel")
    assert refused.code == 2
    assert "REFUSED" in refused.err and "cannot tell whether it runs right now" in refused.err
    assert not credentials.exists()
    taken = run_cli(capsys, "login", "--tunnel-name", "my-tunnel", "--force-takeover")
    assert taken.code == 0, taken.err
    assert "whose live connections Cloudflare did not report" in taken.out and credentials.is_file()


def test_this_machines_own_running_tunnel_needs_no_override(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """The cutover machine: fixed --hostname, tunnel dc-local running from THIS machine, its credentials here (as
    `<name>.json`, or as `<tunnel id>.json` the way plain `cloudflared tunnel create` names it): `login --tunnel-name dc-local`
    exits 0 without --force-takeover and never fetches credentials."""
    first = run_cli(capsys, "login", "--tunnel-name", "dc-local")
    assert first.code == 0, first.err
    fake_cloudflared.connected(account, "dc-local", 4)
    again = run_cli(capsys, "login", "--tunnel-name", "dc-local")
    assert again.code == 0, again.err
    assert "tunnel dc-local: exists" in again.out
    own = layout().cloudflared_dir
    by_id = own / "00000000-0000-0000-0000-0000000000aa.json"
    (own / "dc-local.json").rename(by_id)
    from_id = run_cli(capsys, "login", "--tunnel-name", "dc-local")
    assert from_id.code == 0, from_id.err
    assert "copied to dc-local.json" in from_id.out
    assert (own / "dc-local.json").read_text() == by_id.read_text()
    assert stat.S_IMODE((own / "dc-local.json").stat().st_mode) == 0o600
    assert "0000000000aa" not in from_id.out + from_id.err
    assert not any(" token " in call for call in _calls(account))


def test_credentials_of_another_tunnel_id_are_not_this_machines_tunnel(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """A local my-tunnel.json from a tunnel that was deleted; a same-name machine created my-tunnel again (new id)."""
    run_cli(capsys, "login")
    credentials = layout().cloudflared_dir / "my-tunnel.json"
    stale = credentials.read_text()
    fake_cloudflared.recreated(account, "my-tunnel", "00000000-0000-0000-0000-0000000000bb")
    for extra in ((), ("--tunnel-name", "my-tunnel"), ("--tunnel-name", "my-tunnel", "--force-takeover")):
        refused = run_cli(capsys, "login", *extra)
        assert refused.code == 2
        assert "REFUSED" in refused.err and "is NOT the credentials of the tunnel 'my-tunnel'" in refused.err
        assert credentials.read_text() == stale
    assert not any(" token " in call for call in _calls(account))


@pytest.mark.parametrize("conns", ["null", "missing"])
def test_a_connector_without_connection_data_is_unknown_liveness(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], conns: str
) -> None:
    """A connector listed with `conns: null` (or none at all) may be running: counting it as zero connections would let
    --tunnel-name adopt a tunnel a same-name machine serves."""
    run_cli(capsys, "login")
    credentials = _drop_credentials()
    fake_cloudflared.connector_without_connections(account, "my-tunnel", conns)
    refused = run_cli(capsys, "login", "--tunnel-name", "my-tunnel")
    assert refused.code == 2
    assert "REFUSED" in refused.err and "cannot tell whether it runs right now" in refused.err
    assert not credentials.exists()
    assert not any(" token " in call for call in _calls(account))
    taken = run_cli(capsys, "login", "--tunnel-name", "my-tunnel", "--force-takeover")
    assert taken.code == 0, taken.err
    assert "whose live connections Cloudflare did not report" in taken.out and credentials.is_file()


def test_a_connector_without_connection_data_beside_live_ones_still_reports_them(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """Live connections that are visible are the answer, not "unknown": the refusal must say the tunnel IS running."""
    run_cli(capsys, "login")
    credentials = _drop_credentials()
    fake_cloudflared.connector_without_connections(account, "my-tunnel", "null", active=2)
    refused = run_cli(capsys, "login", "--tunnel-name", "my-tunnel")
    assert refused.code == 2 and "RUNNING right now (2 live connection(s))" in refused.err
    assert not credentials.exists()


def test_a_created_tunnel_whose_credentials_name_no_id_is_never_routed(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DNS is routed by the id in the new tunnel's credentials file; without one there is nothing safe to route to. The file is
    removed, so the advised re-run with --tunnel-name fetches the credentials by id instead of refusing a leftover."""
    monkeypatch.setenv("FAKE_CF_CREDENTIALS", "no-tunnel-id")
    result = run_cli(capsys, "login")
    assert result.code == 1  # AgentToolError: what cloudflared wrote is unusable, nothing of the user's is wrong
    assert "names no valid tunnel id" in result.err and "--tunnel-name my-tunnel" in result.err
    assert not any(" route " in call for call in _calls(account))
    assert fake_cloudflared.routes(account) == {}
    credentials = layout().cloudflared_dir / "my-tunnel.json"
    assert not credentials.exists()
    monkeypatch.delenv("FAKE_CF_CREDENTIALS")
    adopted = run_cli(capsys, "login", "--tunnel-name", "my-tunnel")
    assert adopted.code == 0, adopted.err
    assert "adopted" in adopted.out and fake_cloudflared.routes(account) == {HOST: "my-tunnel"}


@pytest.mark.parametrize("content", [b"\xff\xfe not json", b"[" * 100_000], ids=["not-utf8", "nested-too-deep"])
def test_a_credentials_file_that_cannot_be_read_is_refused_not_a_crash(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], content: bytes
) -> None:
    run_cli(capsys, "login")
    credentials = layout().cloudflared_dir / "my-tunnel.json"
    credentials.chmod(0o600)
    credentials.write_bytes(content)
    refused = run_cli(capsys, "login")
    assert refused.code == 2 and "is NOT the credentials of the tunnel 'my-tunnel'" in refused.err


def test_adopting_by_id_never_writes_through_a_dangling_link(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], tmp_path: Path
) -> None:
    """`<name>.json` linked to a file in ~/.cloudflared that is gone: the copy of `<tunnel id>.json` replaces the link and
    creates nothing where it pointed."""
    run_cli(capsys, "login")
    own = layout().cloudflared_dir
    credentials = own / "my-tunnel.json"
    (own / f"{_tunnel_id_in(credentials)}.json").write_text(credentials.read_text())
    _drop_credentials()
    gone = tmp_path / "dot-cloudflared" / "my-tunnel.json"
    credentials.symlink_to(gone)
    adopted = run_cli(capsys, "login")
    assert adopted.code == 0, adopted.err
    assert not credentials.is_symlink() and credentials.is_file()
    assert not gone.parent.exists()


def test_a_file_named_by_the_tunnel_id_that_names_another_tunnel_is_not_adopted(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """Only an old file WITHOUT TunnelID is bound by its name: one whose TunnelID is another id is neither copied nor given
    the listed id."""
    run_cli(capsys, "login")
    own = layout().cloudflared_dir
    tunnel_id = _tunnel_id_in(own / "my-tunnel.json")
    credentials = _drop_credentials()
    (own / f"{tunnel_id}.json").write_text(json.dumps({**LEGACY, "TunnelID": OTHER_ID}))
    refused = run_cli(capsys, "login")
    assert refused.code == 2 and "no credentials for it" in refused.err
    assert not credentials.exists()


def test_dns_is_routed_to_the_checked_tunnel_id_even_when_the_config_names_other_credentials(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], tmp_path: Path
) -> None:
    """cloudflared resolves a tunnel NAME through the credentials-file of its config first: with --cloudflared-dir at a config
    that names another tunnel's credentials, `route dns my-tunnel` would point the hostname at that other tunnel. Every route
    (new record, already routed, replaced with --yes) must name the id of the tunnel that was checked."""
    fake_cloudflared.recreated(account, "other", OTHER_ID)
    other_credentials = tmp_path / "other.json"
    other_credentials.write_text(json.dumps({"AccountTag": "acct", "TunnelSecret": "b3RoZXI=", "TunnelID": OTHER_ID}))
    own = layout().cloudflared_dir
    own.mkdir(parents=True, mode=0o700)
    (own / "config.yml").write_text(f"no-autoupdate: true\ncredentials-file: {other_credentials}\n")
    created = run_cli(capsys, "login")
    assert created.code == 0, created.err
    my_id = _tunnel_id_in(own / "my-tunnel.json")
    assert my_id != OTHER_ID
    assert fake_cloudflared.routes(account) == {HOST: "my-tunnel"}
    again = run_cli(capsys, "login")
    assert again.code == 0 and "already routed" in again.out
    fake_cloudflared.existing_record(account, HOST, "someone-elses-tunnel")
    replaced = run_cli(capsys, "login", "--yes")
    assert replaced.code == 0, replaced.err
    assert fake_cloudflared.routes(account) == {HOST: "my-tunnel"}
    routed = [call.split() for call in _calls(account) if " route dns " in call]
    assert len(routed) == 4 and all(words[-2] == my_id for words in routed)
    assert not any(my_id in printed for printed in (created.out, created.err, again.out, replaced.out))


def test_a_legacy_credentials_file_named_by_the_tunnel_id_is_migrated(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """An old `cloudflared tunnel create` left `<tunnel id>.json` without TunnelID: its file name binds it to the tunnel, so it
    becomes `<name>.json` with the checked id added (cloudflared reads both shapes); no token fetch, nothing identifying
    printed."""
    run_cli(capsys, "login")
    own = layout().cloudflared_dir
    tunnel_id = _tunnel_id_in(own / "my-tunnel.json")
    _drop_credentials()
    by_id = own / f"{tunnel_id}.json"
    _write_legacy(by_id)
    migrated = run_cli(capsys, "login")
    assert migrated.code == 0, migrated.err
    assert "TunnelID added" in migrated.out
    assert json.loads((own / "my-tunnel.json").read_text()) == {**LEGACY, "TunnelID": tunnel_id}
    assert stat.S_IMODE((own / "my-tunnel.json").stat().st_mode) == 0o600
    assert json.loads(by_id.read_text()) == LEGACY  # the user's own file is left as it was
    assert tunnel_id not in migrated.out + migrated.err
    assert not any(" token " in call for call in _calls(account))
    again = run_cli(capsys, "login")
    assert again.code == 0 and "tunnel my-tunnel: exists" in again.out


def test_a_legacy_credentials_file_under_the_tunnel_name_has_a_way_back(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """CUTOVER.md copies an old `<tunnel id>.json` as `<name>.json`. Without a TunnelID nothing in it says which tunnel it is,
    so it is refused (even with --force-takeover) with a fix that works: a copy named by the tunnel id binds it, and the next
    run adds the TunnelID. A copy under the id whose secret differs does not."""
    run_cli(capsys, "login")
    own = layout().cloudflared_dir
    credentials = own / "my-tunnel.json"
    tunnel_id = _tunnel_id_in(credentials)
    _write_legacy(credentials)
    for extra in ((), ("--tunnel-name", "my-tunnel", "--force-takeover")):
        refused = run_cli(capsys, "login", *extra)
        assert refused.code == 2
        assert "REFUSED" in refused.err and "old credentials file without a TunnelID" in refused.err
        assert "the name cloudflared gave it" in refused.err and "<tunnel id>.json" in refused.err
        assert "Never name a copy after the id Cloudflare lists now" in refused.err
        assert json.loads(credentials.read_text()) == LEGACY
    by_id = own / f"{tunnel_id}.json"
    _write_legacy(by_id, secret="ZGlmZmVyZW50")
    assert run_cli(capsys, "login").code == 2
    assert json.loads(credentials.read_text()) == LEGACY
    _write_legacy(by_id)
    fixed = run_cli(capsys, "login")
    assert fixed.code == 0, fixed.err
    assert "TunnelID added" in fixed.out and tunnel_id not in fixed.out + fixed.err
    assert json.loads(credentials.read_text()) == {**LEGACY, "TunnelID": tunnel_id}
    assert not any(" token " in call for call in _calls(account))


def test_a_legacy_name_file_beside_a_new_file_of_the_tunnel_id_gets_its_tunnel_id(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """The tunnel's own `<tunnel id>.json` already carries TunnelID and the same secret: the old `<name>.json` is that file."""
    run_cli(capsys, "login")
    own = layout().cloudflared_dir
    credentials = own / "my-tunnel.json"
    tunnel_id = _tunnel_id_in(credentials)
    (own / f"{tunnel_id}.json").write_text(credentials.read_text())
    _write_legacy(credentials)
    migrated = run_cli(capsys, "login")
    assert migrated.code == 0, migrated.err
    assert json.loads(credentials.read_text()) == {**LEGACY, "TunnelID": tunnel_id}


def test_old_credentials_of_a_tunnel_created_again_elsewhere_are_never_bound(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str]
) -> None:
    """A same-name machine deleted the tunnel and created it again (new id): the old `<old id>.json` without TunnelID, alone or
    with an old `<name>.json` copied from it, never binds to the new tunnel, not even with --force-takeover, and the refusal
    says what likely happened and how to get past it."""
    run_cli(capsys, "login")
    own = layout().cloudflared_dir
    old_id = _tunnel_id_in(own / "my-tunnel.json")
    credentials = _drop_credentials()
    _write_legacy(own / f"{old_id}.json")
    fake_cloudflared.recreated(account, "my-tunnel", OTHER_ID)
    calls_before = len(fake_cloudflared.calls(account))
    alone = run_cli(capsys, "login")
    assert alone.code == 2 and "no credentials for it" in alone.err
    assert not credentials.exists()
    _write_legacy(credentials)
    for extra in ((), ("--tunnel-name", "my-tunnel", "--force-takeover")):
        refused = run_cli(capsys, "login", *extra)
        assert refused.code == 2 and "old credentials file without a TunnelID" in refused.err
        assert "created again" in refused.err and "--tunnel-name my-tunnel" in refused.err
        assert json.loads(credentials.read_text()) == LEGACY
    new = [" ".join(call.argv) for call in fake_cloudflared.calls(account)[calls_before:]]
    assert not any(" token " in call or " route " in call for call in new)


def test_migrating_a_linked_old_credentials_file_never_writes_through_the_link(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], tmp_path: Path
) -> None:
    """Both files linked to the original in ~/.cloudflared (which is never written): the migration gives `<name>.json` a file
    of its own and leaves the original as it was."""
    run_cli(capsys, "login")
    own = layout().cloudflared_dir
    credentials = own / "my-tunnel.json"
    tunnel_id = _tunnel_id_in(credentials)
    original = tmp_path / "dot-cloudflared" / f"{tunnel_id}.json"
    original.parent.mkdir()
    _write_legacy(original)
    _drop_credentials()
    credentials.symlink_to(original)
    (own / f"{tunnel_id}.json").symlink_to(original)
    migrated = run_cli(capsys, "login")
    assert migrated.code == 0, migrated.err
    assert not credentials.is_symlink() and json.loads(credentials.read_text()) == {**LEGACY, "TunnelID": tunnel_id}
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert json.loads(original.read_text()) == LEGACY


def test_yes_never_replaces_the_record_of_a_domain_hostname(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A's 0.2.x tunnel name serves pc-mcp.example.test; B (also `pc`) derives the same hostname from --domain."""
    host = "pc-mcp.example.test"
    save_settings(Settings(hostname=host, domain="example.test", machine_name="pc", tunnel_name="pc"))
    fake_cloudflared.existing_record(account, host, "computer-pc-mcp-example-test")
    # an exported cloudflared overwrite switch must not bypass the refusal either
    monkeypatch.setenv("TUNNEL_FORCE_PROVISIONING_DNS", "true")
    for extra in ((), ("--yes",)):
        refused = run_cli(capsys, "login", *extra)
        assert refused.code == 2
        assert f"REFUSED: the DNS record {host} already exists" in refused.err
        assert "--yes does not replace it" in refused.err and "--force-takeover" in refused.err
        assert "--machine-name" in refused.err
        assert fake_cloudflared.routes(account) == {host: "computer-pc-mcp-example-test"}
    assert not any("--overwrite-dns" in call for call in _calls(account))
    taken = run_cli(capsys, "login", "--force-takeover")
    assert taken.code == 0, taken.err
    assert "WARNING: --force-takeover" in taken.out and "REPLACED" in taken.out
    assert fake_cloudflared.routes(account) == {host: "pc"}


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


def _token_cert(payload: bytes | str, blank: str = "") -> str:
    """A token block around `payload` (JSON text or raw bytes); `blank` is put between the two halves of the base64 body."""
    body = base64.b64encode(payload.encode() if isinstance(payload, str) else payload).decode()
    half = len(body) // 2 // 4 * 4
    return f"-----BEGIN ARGO TUNNEL TOKEN-----\n{body[:half]}{blank}{body[half:]}\n-----END ARGO TUNNEL TOKEN-----\n"


TOKEN = '"zoneID":"z","accountID":"a","apiToken":"t"'


@pytest.mark.parametrize(
    ("content", "why"),
    [
        # encoding/json fills apiToken from APITOKEN too, last one wins: cloudflared would read an empty token
        (_token_cert('{"zoneID":"z","accountID":"a","apiToken":"t","APITOKEN":""}'), "apitoken more than once"),
        (_token_cert('{"zoneID":"z","ZONEID":"y","accountID":"a","apiToken":"t"}'), "zoneid more than once"),
        (_token_cert('{"zoneID":"z","accountid":"b","accountID":"a","apiToken":"t"}'), "accountid more than once"),
        # the Kelvin sign folds onto k in Go's foldName
        (_token_cert('{"zoneID":"z","accountID":"a","apiToken":"t","apiTo\\u212aen":""}'), "apitoken more than once"),
        # the later value is a good string, but encoding/json still fails on the earlier number
        (_token_cert('{"zoneID":"z","accountID":"a","apiToken":1,"apiToken":"t"}'), "apitoken of its token is not a string"),
        (_token_cert('{"zoneID":"z","accountID":"a","APIToken":["t"]}'), "apitoken of its token is not a string"),
        (_token_cert("{" + TOKEN + ',"other":NaN}'), "not base64 of a UTF-8 JSON object"),
        (_token_cert(b'{"zoneID":"z\xff","accountID":"a","apiToken":"t"}'), "not base64 of UTF-8 text"),
        (_token_cert(b"\xef\xbb\xbf{" + TOKEN.encode() + b"}"), "not base64 of a UTF-8 JSON object"),
        (_token_cert(("{" + TOKEN + "}").encode("utf-16")), "not base64 of UTF-8 text"),
        (_token_cert("[" + TOKEN.replace(":", ",") + "]"), "not base64 of a UTF-8 JSON object"),
        # one level past the bound (the token object is level 1); brackets inside strings do not count
        (_token_cert("{" + TOKEN + ',"x":' + "[" * 100 + "]" * 100 + "}"), "deeper than 100 levels"),
        (_token_cert("{" + TOKEN + "}").replace("\n-----END", "=\n-----END"), "not base64 of UTF-8 text"),
        # pem.Decode strips only spaces and tabs and base64 skips only line breaks: these make cloudflared find no token
        (_token_cert("{" + TOKEN + "}", "\v"), "broken only by spaces, tabs and line breaks"),
        (_token_cert("{" + TOKEN + "}", "\f"), "broken only by spaces, tabs and line breaks"),
        (_token_cert("{" + TOKEN + "}", "\u00a0"), "broken only by spaces, tabs and line breaks"),
        (_token_cert('{"zoneID":"z","accountID":"a","apiToken":""}'), "no apiToken"),
        # the dotless i and the dotted capital I fold only onto themselves in Go: these keys are ignored, so no token at all
        (_token_cert('{"zoneID":"z","accountID":"a","ap\\u0131Token":"t"}'), "no apiToken"),
        (_token_cert('{"zoneID":"z","accountID":"a","AP\\u0130TOKEN":"t"}'), "no apiToken"),
    ],
    ids=[
        "apiToken-APITOKEN",
        "zoneID-ZONEID",
        "accountid-accountID",
        "kelvin-sign-alias",
        "earlier-mistyped-duplicate",
        "array-token",
        "nan",
        "non-utf8",
        "utf8-bom",
        "utf16",
        "json-array",
        "nested-too-deep",
        "excess-padding",
        "vertical-tab",
        "form-feed",
        "no-break-space",
        "empty-token",
        "dotless-i-key-ignored",
        "dotted-capital-i-key-ignored",
    ],
)
def test_a_token_cloudflared_would_not_decode_is_refused_before_cert_pem_is_written(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], tmp_path: Path, content: str, why: str
) -> None:
    """The Go decoders of the pinned cloudflared decide: encoding/pem, then encoding/json matching field names ignoring case."""
    bad = tmp_path / "bad.pem"
    bad.write_text(content)
    refused = run_cli(capsys, "login", "--cloudflare-cert", str(bad))
    assert refused.code == 2 and "is not a Cloudflare account certificate" in refused.err
    assert why in refused.err
    assert not (layout().cloudflared_dir / "cert.pem").exists()
    assert not any(c.argv[:2] == ["tunnel", "login"] for c in fake_cloudflared.calls(account))


@pytest.mark.parametrize(
    "content",
    [
        _token_cert("{" + TOKEN + "}", " \t\n  "),  # pem.Decode drops spaces and tabs, base64 the line break
        _token_cert('{"zoneID":"z","accountID":"a","apiToken":"t","endpoint":null,"apiTokens":1,"extra":{"apiToken":2}}'),
        _token_cert("{" + TOKEN + ',"ap\\u0131Token":"","AP\\u0130TOKEN":""}'),  # ignored by encoding/json, not repeats
        account_cert("a").replace("-----\n", "-----\r\n"),
        _token_cert("{" + TOKEN + ',"x":' + "[" * 99 + "]" * 99 + ',"y":"' + "[{" * 200 + '\\""}'),  # exactly at the bound
    ],
    ids=["spaces-tabs-newlines", "unknown-and-nested-keys", "dotted-and-dotless-i-keys", "crlf", "nesting-at-the-bound"],
)
def test_a_token_cloudflared_decodes_is_imported(
    capsys: pytest.CaptureFixture[str], account: Path, opened: list[str], tmp_path: Path, content: str
) -> None:
    good = tmp_path / "cert.pem"
    good.write_text(content)
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
