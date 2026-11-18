"""Cloudflare named-tunnel setup around the pinned `cloudflared`: login, create the tunnel, route the hostname.

WHAT: idempotent steps, each skipped when it is already done. cloudflared's state for this target lives in ONE directory of its
own (`Layout.cloudflared_dir`, default ~/.config/computer/chatgpt/cloudflared): the account certificate `cert.pem`, one
`<tunnel>.json` credentials file per tunnel and a `config.yml` passed as --config to every call. `~/.cloudflared` (which may serve
another tunnel on this machine) is never read or written unless the user points `--cloudflared-dir` at it.
HOW THE LOGIN STAYS OUT OF ~/.cloudflared: `cloudflared tunnel login` has no output-path flag and writes
`$HOME/.cloudflared/cert.pem`, refusing when one exists. It runs with HOME set to a staging directory inside ours (and an empty
PATH, so it cannot start a browser under that fake HOME); the login URL it prints is opened with the user's real environment, and
the certificate is then moved into place with mode 0600.
INVARIANTS: only the pinned binary is run; nothing secret or identifying (certificate, credentials, tunnel id) is printed; a DNS
record that points elsewhere is replaced only with the caller's consent (`route_dns(..., overwrite=True)`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agenttools_errors import AgentToolError, NetworkError, PermissionDeniedError

from computerlib.fileio import atomic_write
from computerlib.targets.chatgpt.shim.jsonish import as_list, as_object

LOGIN_URL = re.compile(r"https://\S+argotunnel\S*")
CONFIG_TEXT = (
    "# Managed by `computer chatgpt`: passed as --config to every cloudflared call of this target, "
    "so cloudflared never falls back\n"
    "# to ~/.cloudflared/config.yml. Tunnel name, credentials and origin are given on the command line.\n"
    "no-autoupdate: true\n"
)

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
LoginRunner = Callable[[list[str], dict[str, str], Callable[[str], None]], int]
Opener = Callable[[str], bool]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "cloudflared did not answer within 120 s")


def _stream(argv: list[str], env: dict[str, str], on_line: Callable[[str], None]) -> int:
    """Run `argv`, handing every output line to `on_line` as it arrives (cloudflared waits for the browser meanwhile)."""
    with subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace") as child:
        assert child.stdout is not None
        for line in child.stdout:
            on_line(line.rstrip("\n"))
        return child.wait()


def open_in_browser(url: str) -> bool:
    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener is None:
        return False
    subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


class Route(Enum):
    CREATED = "created"
    UNCHANGED = "unchanged"  # the record already points at this tunnel
    CONFLICT = "conflict"  # a record with this name exists and points elsewhere (another tunnel, an A/AAAA record, ...)


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return ((result.stderr or "") + (result.stdout or "")).strip()[-400:] or f"exit {result.returncode}"


@dataclass(frozen=True)
class Cloudflare:
    binary: Path
    directory: Path
    runner: Runner = _run
    login_runner: LoginRunner = _stream
    opener: Opener | None = None  # None: open_in_browser, looked up at call time so a test can stand in for the browser

    @property
    def cert(self) -> Path:
        return self.directory / "cert.pem"

    @property
    def config(self) -> Path:
        return self.directory / "config.yml"

    def credentials(self, name: str) -> Path:
        return self.directory / f"{name}.json"

    def prepare(self) -> None:
        """The private directory and the config.yml every call names (written once; a user's own edits are kept)."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.config.exists():
            atomic_write(self.config, CONFIG_TEXT, mode=0o600)

    def _tunnel(self, *rest: str) -> list[str]:
        return [str(self.binary), "tunnel", "--config", str(self.config), "--origincert", str(self.cert), *rest]

    def logged_in(self) -> bool:
        return self.cert.is_file()

    def login(self, out: Callable[[str], None] = print) -> None:
        self.prepare()
        staging = self.directory / "login-home"
        staged = staging / ".cloudflared" / "cert.pem"
        opened = False

        def on_line(line: str) -> None:
            nonlocal opened
            out(f"  cloudflared: {line}" if line else "")
            found = LOGIN_URL.search(line)
            if found and not opened:
                opened = True
                if not (self.opener or open_in_browser)(found.group(0)):
                    out("  (no browser opener found: open the URL above yourself)")

        if not staged.is_file():
            staging.mkdir(parents=True, exist_ok=True, mode=0o700)
            # An empty PATH keeps cloudflared from launching a browser itself under the staging HOME (a browser started there
            # would get a fresh profile without the user's Cloudflare session); we open the URL with the real environment.
            env = {**os.environ, "HOME": str(staging), "PATH": ""}  # proxies and the like still apply
            code = self.login_runner([str(self.binary), "tunnel", "login"], env, on_line)
            if code != 0 and not staged.is_file():
                raise PermissionDeniedError(
                    what="the Cloudflare login did not finish",
                    why=f"cloudflared exited {code} without writing an account certificate",
                    fix="re-run the command and authorize the zone of your hostname in the browser page cloudflared shows",
                )
        if not staged.is_file():
            raise PermissionDeniedError(
                what="the Cloudflare login did not finish",
                why="no account certificate was written",
                fix="re-run the command and authorize the zone of your hostname in the browser page cloudflared shows",
            )
        os.chmod(staged, 0o600)
        os.replace(staged, self.cert)
        shutil.rmtree(staging)

    def tunnel_exists(self, name: str) -> bool:
        result = self.runner(self._tunnel("list", "--output", "json", "--name", name))
        if result.returncode != 0:
            raise NetworkError(what="cannot list Cloudflare tunnels", why=_detail(result), fix="check the login and the network")
        try:
            listed = as_list(json.loads(result.stdout or "[]"))
        except ValueError:
            return False
        return any(as_object(item).get("name") == name for item in listed)

    def create_tunnel(self, name: str) -> None:
        result = self.runner(self._tunnel("create", "--credentials-file", str(self.credentials(name)), name))
        if result.returncode != 0 or not self.credentials(name).is_file():
            raise AgentToolError(
                what=f"cannot create the tunnel {name!r}", why=_detail(result), fix="check the Cloudflare login and the network"
            )

    def fetch_credentials(self, name: str) -> None:
        """Credentials of a tunnel that exists on the account but not in this directory (created elsewhere, or files lost)."""
        result = self.runner(self._tunnel("token", "--cred-file", str(self.credentials(name)), name))
        if result.returncode != 0 or not self.credentials(name).is_file():
            raise AgentToolError(
                what=f"cannot fetch the credentials of the existing tunnel {name!r}",
                why=_detail(result),
                fix="use another --tunnel-name (a tunnel created before cloudflared 2022.3 has no fetchable token)",
            )

    def route_dns(self, name: str, hostname: str, *, overwrite: bool = False) -> Route:
        """Point `hostname` at the tunnel. Without `overwrite` an existing record that points elsewhere is left alone
        (CONFLICT)."""
        result = self.runner(self._tunnel("route", "dns", *(["--overwrite-dns"] if overwrite else []), name, hostname))
        text = (result.stderr or "") + (result.stdout or "")
        if result.returncode == 0:
            return Route.UNCHANGED if "already configured to route" in text else Route.CREATED
        if "already exists" in text.lower() and not overwrite:
            return Route.CONFLICT
        raise AgentToolError(
            what=f"cannot route {hostname} to the tunnel {name!r}",
            why=_detail(result),
            fix="the hostname must be in a zone on the Cloudflare account you logged in with (the zone chosen in the browser)",
        )
