"""Cloudflare named-tunnel setup around the pinned `cloudflared`: login, create the tunnel, route the hostname.

WHAT: idempotent steps, each skipped when it is already done. cloudflared's state for this target lives in ONE directory of its
own (`Layout.cloudflared_dir`, default ~/.config/computer/chatgpt/cloudflared): the account certificate `cert.pem`, one
`<tunnel>.json` credentials file per tunnel and a `config.yml` passed as --config to every call. `~/.cloudflared` (which may serve
another tunnel on this machine) is never read or written unless the user points `--cloudflared-dir` at it.
HOW THE LOGIN STAYS OUT OF ~/.cloudflared: `cloudflared tunnel login` has no output-path flag and writes
`$HOME/.cloudflared/cert.pem`, refusing when one exists. It runs with HOME set to a staging directory inside ours (and an empty
PATH, so it cannot start a browser under that fake HOME); the login URL it prints is shown again on its own line (flushed, for a
headless machine over ssh: it can be opened on any device) and opened with the user's real environment when there is a display,
and the certificate is then moved into place with mode 0600. `import_cert` instead reuses a certificate from an earlier login,
read and checked once up front (`read_account_cert`), before the caller writes anything.
INVARIANTS: only the pinned binary is run; nothing secret or identifying (certificate, credentials, tunnel id) is printed; a DNS
record that points elsewhere is replaced only with the caller's consent (`route_dns(..., overwrite=True)`); a credentials file
counts as a tunnel's only when its TunnelID is the id `tunnel list` shows (`CredentialsFile.tunnel_id`), or, for an old file
without TunnelID, when its file name is that id (`<tunnel id>.json`, as cloudflared named it); every call about an existing
tunnel (`info`, `token`, `route dns`) names it by that id, never by name, since cloudflared resolves a name through the
credentials-file of its config first.
"""

from __future__ import annotations

import binascii
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agenttools_errors import AgentToolError, ConfigError, MissingTargetError, NetworkError, PermissionDeniedError, UsageError

from computerlib.fileio import atomic_write
from computerlib.targets.chatgpt.shim.jsonish import as_list, as_object

LOGIN_URL = re.compile(r"https://\S+argotunnel\S*")
# What `cloudflared tunnel login` writes: one PEM block whose body is base64 JSON with these string fields (cloudflared's
# credentials.OriginCertToken).
# Delimiters as Go's pem.Decode reads them: on their own line, trailing spaces and tabs allowed, any type (hyphens too); a
# block collapsed onto one line is no block. No nested quantifiers over the same whitespace, so a malformed file fails fast.
# The body holds only what pem.Decode decodes: the base64 alphabet and the spaces, tabs and line breaks it skips. Any other
# blank (a vertical tab, a form feed, a no-break space) fails its base64 decoding, so cloudflared would find no token.
ACCOUNT_CERT = re.compile(
    r"^-----BEGIN ARGO TUNNEL TOKEN-----[ \t]*$(?P<body>[A-Za-z0-9+/= \t\r\n]*?)^-----END ARGO TUNNEL TOKEN-----[ \t]*$",
    re.MULTILINE,
)
PEM_BLANKS = str.maketrans("", "", " \t\r\n")
PEM_BEGIN = re.compile(r"^-----BEGIN (?P<type>[^\n]*?)-----[ \t]*$", re.MULTILINE)
# The block types cloudflared's decodeOriginCert accepts (the legacy layout adds a key and a certificate); any other is an error.
PEM_TYPES = frozenset({"ARGO TUNNEL TOKEN", "PRIVATE KEY", "CERTIFICATE"})
ACCOUNT_CERT_FIELDS = ("zoneID", "accountID", "apiToken")
# Every string field of OriginCertToken (lowercase); encoding/json fails on a non-string value for one of them.
ACCOUNT_CERT_STRINGS = frozenset({"zoneid", "accountid", "apitoken", "endpoint"})
# encoding/json gives a key to a field when foldName makes them equal: ASCII letters uppercased, any other rune replaced by the
# smallest rune of its unicode.SimpleFold orbit. Only two orbits hold a non-ASCII rune and an ASCII letter (long s with S, the
# Kelvin sign with K); the dotted capital I and the dotless i fold only onto themselves, so a key spelled with them is ignored.
GO_FOLD = str.maketrans({"\u017f": "S", "\u212a": "K"})
CONFIG_TEXT = (
    "# Managed by `computer chatgpt`: passed as --config to every cloudflared call of this target, "
    "so cloudflared never falls back\n"
    "# to ~/.cloudflared/config.yml. Tunnel name, credentials and origin are given on the command line.\n"
    "no-autoupdate: true\n"
)
TUNNEL_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
LoginRunner = Callable[[list[str], dict[str, str], Callable[[str], None]], int]
Opener = Callable[[str], bool]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    # cloudflared reads TUNNEL_FORCE_PROVISIONING_DNS as --overwrite-dns: an exported one would replace a record that the caller
    # was never allowed to replace (route_dns(..., overwrite=True) is the only way).
    env = {key: value for key, value in os.environ.items() if key != "TUNNEL_FORCE_PROVISIONING_DNS"}
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120, env=env)
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
    """Open `url` in this machine's browser; False on a headless machine (no display) or without an opener."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener is None:
        return False
    subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def _say(text: str) -> None:
    # Flushed: over ssh or into a pipe stdout is block-buffered, and the login URL must show while cloudflared waits.
    print(text, flush=True)


class Route(Enum):
    CREATED = "created"
    UNCHANGED = "unchanged"  # the record already points at this tunnel
    CONFLICT = "conflict"  # a record with this name exists and points elsewhere (another tunnel, an A/AAAA record, ...)


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return ((result.stderr or "") + (result.stdout or "")).strip()[-400:] or f"exit {result.returncode}"


class Adoption(Enum):
    COPIED = "copied"
    MIGRATED = "migrated"  # an old file without TunnelID: its file name `<tunnel id>.json` gave the id, now written into it


# What an old credentials file without TunnelID holds; the two must match for an old `<name>.json` to be a `<tunnel id>.json`.
LEGACY_FIELDS = ("AccountTag", "TunnelSecret")


@dataclass(frozen=True)
class CredentialsFile:
    """A tunnel credentials file read once: its text and JSON object (empty when missing, unreadable, not UTF-8 or not an
    object), so every decision about it and whatever is copied from it are the same content."""

    text: str
    fields: dict[str, object]

    @property
    def tunnel_id(self) -> str | None:
        """Its TunnelID (lowercase); None when it has none."""
        value = self.fields.get("TunnelID")
        return value.lower() if isinstance(value, str) and value else None

    @property
    def legacy(self) -> bool:
        """An old file: AccountTag and TunnelSecret but no TunnelID (absent or null). Older cloudflared releases wrote these,
        and cloudflared still runs them, taking the id from the tunnel it is asked to run."""
        return self.fields.get("TunnelID") is None and all(
            isinstance(value := self.fields.get(key), str) and value for key in LEGACY_FIELDS
        )

    def with_tunnel_id(self, tunnel_id: str) -> str:
        # The id cloudflared would take from the tunnel it runs, written down: from now on the file is checked like a new one.
        return json.dumps({**self.fields, "TunnelID": tunnel_id})


def read_credentials(path: Path) -> CredentialsFile:
    try:
        text = path.read_text()
    except (OSError, ValueError):  # ValueError: UnicodeDecodeError, a file that is no credentials file at all
        return CredentialsFile("", {})
    try:
        return CredentialsFile(text, as_object(json.loads(text)))
    except (ValueError, RecursionError):  # RecursionError: brackets nested deeper than Python's decoder goes
        return CredentialsFile(text, {})


@dataclass(frozen=True)
class _JsonObject:
    """A decoded JSON object with every key as written, repeats included: a dict keeps only the last of a repeated key."""

    pairs: list[tuple[str, object]]


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")  # Python's json accepts NaN and Infinity, encoding/json does not


def _token_field(key: str) -> str | None:
    """The OriginCertToken string field (lowercase) encoding/json decodes `key` into, or None for a key it ignores."""
    folded = "".join(char.upper() if char.isascii() else char for char in key.translate(GO_FOLD))
    return folded.lower() if folded.isascii() and folded.lower() in ACCOUNT_CERT_STRINGS else None


# encoding/json allows 10000 levels, but Python's decoder recurses and overflows near 1000 on some versions and not on others;
# a bound well below both gives the same answer on every Python. A real token is one flat object.
TOKEN_DEPTH = 100


def _json_depth(text: str) -> int:
    """How deep arrays and objects nest in the JSON `text` (brackets inside strings do not count)."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def _decode_token(body: str) -> _JsonObject | str:
    """The JSON object in the base64 `body` of the token block, or why there is none."""
    try:
        # Strict UTF-8: a byte-order mark or UTF-16 is a syntax error to encoding/json, though json.loads(bytes) takes it.
        payload = binascii.a2b_base64(body.translate(PEM_BLANKS), strict_mode=True).decode()
    except (binascii.Error, ValueError):  # UnicodeDecodeError is a ValueError
        return "its token block is not base64 of UTF-8 text"
    if _json_depth(payload) > TOKEN_DEPTH:
        return f"its token nests JSON deeper than {TOKEN_DEPTH} levels"
    try:
        token = json.loads(payload, object_pairs_hook=_JsonObject, parse_constant=_no_constant)
    except ValueError:  # JSONDecodeError is a ValueError
        token = None
    return token if isinstance(token, _JsonObject) else "its token block is not base64 of a UTF-8 JSON object"


def _token_problem(token: _JsonObject) -> str | None:
    """Why encoding/json would not fill OriginCertToken from `token` with every field cloudflared needs, or None."""
    fields: dict[str, object] = {}
    for key, value in token.pairs:
        field = _token_field(key)
        if field is None:
            continue
        # encoding/json matches names ignoring case and the last value wins, but a wrongly typed earlier one still fails the
        # decoding: a repeated field is refused outright instead of guessing which value cloudflared would end up with.
        if field in fields:
            return f"its token sets the field {field} more than once (cloudflared reads field names ignoring case)"
        if not isinstance(value, str | None):
            return f"the field {field} of its token is not a string"
        fields[field] = value
    missing = [name for name in ACCOUNT_CERT_FIELDS if not fields.get(name.lower())]
    return f"its token has no {', '.join(missing)}, which cloudflared needs" if missing else None


def _account_cert_problem(text: str) -> str | None:
    """Why the pinned cloudflared would not read `text` as an account certificate, or None when it would."""
    # cloudflared refuses a file with more than one token block or an unknown block (credentials/origin_cert.go).
    blocks = ACCOUNT_CERT.findall(text)
    types = [found.group("type") for found in PEM_BEGIN.finditer(text)]
    if not (len(blocks) == 1 and types.count("ARGO TUNNEL TOKEN") == 1 and set(types) <= PEM_TYPES):
        return (
            "it needs exactly one ARGO TUNNEL TOKEN block, each on its own lines, in base64 broken only by spaces, tabs "
            "and line breaks, and no block of another kind"
        )
    token = _decode_token(blocks[0])
    return token if isinstance(token, str) else _token_problem(token)


def _require_account_cert(text: str, source: Path) -> None:
    """`text` must be what `cloudflared tunnel login` writes, or a usage error: a published certificate means "logged in"."""
    problem = _account_cert_problem(text)
    if problem is not None:
        raise UsageError(
            what=f"{source} is not a Cloudflare account certificate",
            why=problem,
            fix="pass the cert.pem that `cloudflared tunnel login` wrote (e.g. ~/.cloudflared/cert.pem on another machine)",
        )


@dataclass(frozen=True)
class AccountCert:
    """An account certificate read from `source` and checked: read once, so `/dev/stdin` or a process substitution works."""

    source: Path
    text: str


def read_account_cert(source: Path) -> AccountCert:
    try:
        text = source.expanduser().read_text()
    except (OSError, ValueError) as error:
        raise MissingTargetError(
            what=f"cannot read the Cloudflare certificate {source}",
            why=str(error),
            fix="pass the cert.pem that `cloudflared tunnel login` wrote (e.g. ~/.cloudflared/cert.pem on another machine)",
        ) from error
    _require_account_cert(text, source)  # a published certificate means "logged in": never publish one cloudflared rejects
    return AccountCert(source, text)


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

    def manual_route_command(self, hostname: str) -> str:
        """The `route dns` a user runs by hand (--skip-dns): this target's binary, config and certificate, and the tunnel by
        id, since a bare `cloudflared` reads ~/.cloudflared/config.yml, whose credentials-file may resolve a name to another
        tunnel. No id is printed: the shell takes it from $COMPUTER_TUNNEL_ID (a name of this tool's, so no leftover generic
        variable passes) and stops when it is unset or empty, because any word that is not a UUID (an unedited placeholder
        too) is such a name to cloudflared."""
        tunnel = '"${COMPUTER_TUNNEL_ID:?set COMPUTER_TUNNEL_ID to the tunnel id}"'
        return f"{shlex.join(self._tunnel('route', 'dns'))} {tunnel} {shlex.quote(hostname)}"

    def logged_in(self) -> bool:
        return self.cert.is_file()

    def _already_published(self, prepared: Path) -> bool:
        if self.cert.read_bytes() == prepared.read_bytes():
            return False
        raise ConfigError(
            what=f"{self.cert} already holds a different Cloudflare account certificate",
            why="replacing it could move this machine to another account and orphan its tunnel",
            fix=f"remove {self.cert} first if you mean to switch accounts (or drop --cloudflare-cert)",
        )

    def _publish_cert(self, prepared: Path) -> bool:
        """Make `prepared` (a 0600 file on this directory's filesystem) the account certificate. link(2) never replaces, so of
        two concurrent logins or imports exactly one publishes and the other compares: False when the same certificate is
        already there, a different one is refused."""
        try:
            os.link(prepared, self.cert)
            return True
        except FileExistsError:
            return self._already_published(prepared)
        except OSError:
            pass  # no hard links on this filesystem (vfat, exFAT, some FUSE mounts)
        return self._publish_by_rename(prepared)

    def _publish_by_rename(self, prepared: Path) -> bool:
        """The no-hard-link fallback: a complete copy renamed into place (readers never see a partial file) under a lock, so
        checking for an existing certificate and renaming over the gap are one step for concurrent publishers."""
        handle, copy_name = tempfile.mkstemp(dir=self.directory, prefix=".cert.", suffix=".tmp")  # mode 0600
        try:
            with os.fdopen(handle, "wb") as copy:
                copy.write(prepared.read_bytes())
            with open(self.directory / ".cert.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)  # released by the kernel if this process dies
                if self.cert.exists():
                    return self._already_published(prepared)
                os.rename(copy_name, self.cert)
                return True
        finally:
            Path(copy_name).unlink(missing_ok=True)

    def import_cert(self, cert: AccountCert) -> bool:
        """Reuse the account certificate of an earlier `cloudflared tunnel login` (another machine of the same account): copied
        mode 0600. False when this directory already holds the same one; a different one is refused, never replaced."""
        self.prepare()
        handle, temp_name = tempfile.mkstemp(dir=self.directory, prefix=".cert.", suffix=".tmp")  # mode 0600
        try:
            with os.fdopen(handle, "w") as temp:
                temp.write(cert.text)
            return self._publish_cert(Path(temp_name))
        finally:
            os.unlink(temp_name)

    def login(self, out: Callable[[str], None] = _say) -> None:
        self.prepare()
        # One staging HOME per login (mode 0700): a concurrent login writes its certificate into its own directory, never into
        # the file this one publishes by hard link.
        staging = Path(tempfile.mkdtemp(prefix="login-home-", dir=self.directory))
        staged = staging / ".cloudflared" / "cert.pem"
        opened = False

        def on_line(line: str) -> None:
            nonlocal opened
            out(f"  cloudflared: {line}" if line else "")
            found = LOGIN_URL.search(line)
            if found and not opened:
                opened = True
                url = found.group(0)
                out(f"  open this URL in a browser on any device and authorize the zone; waiting for it:\n  {url}")
                if not (self.opener or open_in_browser)(url):
                    out("  (no browser on this machine: open the URL above on another device; the login finishes here)")

        try:
            # An empty PATH keeps cloudflared from launching a browser itself under the staging HOME (a browser started there
            # would get a fresh profile without the user's Cloudflare session); we open the URL with the real environment.
            env = {**os.environ, "HOME": str(staging), "PATH": ""}  # proxies and the like still apply
            code = self.login_runner([str(self.binary), "tunnel", "login"], env, on_line)
            if not staged.is_file():
                raise PermissionDeniedError(
                    what="the Cloudflare login did not finish",
                    why=f"cloudflared exited {code} without writing an account certificate",
                    fix="re-run the command and authorize the zone of your hostname in the browser page cloudflared shows",
                )
            os.chmod(staged, 0o600)
            self._publish_cert(staged)  # a certificate imported or logged in meanwhile is never overwritten
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def find_tunnel(self, name: str) -> str | None:
        """The id (lowercase UUID) of the account's tunnel called `name`, None when there is none."""
        result = self.runner(self._tunnel("list", "--output", "json", "--name", name))
        if result.returncode != 0:
            raise NetworkError(what="cannot list Cloudflare tunnels", why=_detail(result), fix="check the login and the network")
        try:
            listed = as_list(json.loads(result.stdout or "[]"))
        except ValueError:
            return None
        for item in listed:
            fields = as_object(item)
            if fields.get("name") == name:
                tunnel_id = str(fields.get("id") or "").lower()
                # Every later step is bound to this id (it also names a file here), so anything but a UUID is refused.
                if not TUNNEL_UUID.fullmatch(tunnel_id):
                    raise NetworkError(
                        what=f"`cloudflared tunnel list` gave no valid id for the tunnel {name!r}",
                        why="the answer did not have the shape this tool expects",
                        fix="re-run; if it persists, report it with the cloudflared version",
                    )
                return tunnel_id
        return None

    def live_connections(self, tunnel_id: str) -> int | None:
        """How many edge connections the tunnel has open right now (a cloudflared somewhere runs it), from `tunnel info`, which
        reads Cloudflare's connections endpoint: since 2026-10-05 `tunnel list` carries no connections. Recently closed ones
        (`is_pending_reconnect`) do not count, as in cloudflared's own listing. None when the answer cannot be read, or shows no
        live connection while a connector reports none of its own: unknown."""
        result = self.runner(self._tunnel("info", "--output", "json", tunnel_id))
        if result.returncode != 0:
            raise NetworkError(
                what="cannot ask Cloudflare whether the tunnel runs", why=_detail(result), fix="check the login and the network"
            )
        try:
            info = as_object(json.loads(result.stdout or ""))
        except ValueError:
            return None
        if not isinstance(info.get("conns"), list):
            return None
        connectors = [as_object(connector) for connector in as_list(info.get("conns"))]
        live = sum(
            1
            for connector in connectors
            for connection in as_list(connector.get("conns"))
            if not as_object(connection).get("is_pending_reconnect")
        )
        # A connector listed without its own connections (null, or no field) may be serving right now: with no live
        # connection visible elsewhere that is unknown, never zero; beside live ones the visible count is the answer.
        if live == 0 and any(not isinstance(connector.get("conns"), list) for connector in connectors):
            return None
        return live

    def _credentials_named_by_id(self, tunnel_id: str) -> CredentialsFile | None:
        """`<tunnel id>.json` (what plain `cloudflared tunnel create` writes) when it is `tunnel_id`'s: its TunnelID is that id,
        or it is an old file without one, which only its file name binds to a tunnel. None otherwise."""
        found = read_credentials(self.directory / f"{tunnel_id}.json")
        return found if found.tunnel_id == tunnel_id or found.legacy else None

    def adopt_credentials_by_id(self, name: str, tunnel_id: str) -> Adoption | None:
        """`<tunnel id>.json` in this directory, when it is `tunnel_id`'s, is this machine's own credentials file: written to
        `<name>.json` (mode 0600), the file the units name, as it is or, an old one, with the id added. None when there is no
        such file."""
        found = self._credentials_named_by_id(tunnel_id)
        if found is None:
            return None
        if found.tunnel_id:
            self._write_credentials(name, found.text)
            return Adoption.COPIED
        self._write_credentials(name, found.with_tunnel_id(tunnel_id))
        return Adoption.MIGRATED

    def migrate_legacy_credentials(self, name: str, tunnel_id: str, legacy: CredentialsFile) -> bool:
        """`<name>.json` is `legacy`, an old file without TunnelID: nothing in it names a tunnel, so it is bound to `tunnel_id`
        only when `<tunnel id>.json` here (old or new) holds the same account and secret; then the id is added. False
        otherwise."""
        found = self._credentials_named_by_id(tunnel_id)
        if found is None or any(legacy.fields.get(key) != found.fields.get(key) for key in LEGACY_FIELDS):
            return False
        self._write_credentials(name, legacy.with_tunnel_id(tunnel_id))
        return True

    def _write_credentials(self, name: str, text: str) -> None:
        own = self.credentials(name)
        if own.is_symlink():
            # atomic_write writes through a link (a dangling one too), and this one may lead into ~/.cloudflared, which is
            # never written: the link is replaced by a file of its own (lost only if the write fails; `<tunnel id>.json` then
            # adopts again). missing_ok: a concurrent run may have replaced it already.
            own.unlink(missing_ok=True)
        atomic_write(own, text, mode=0o600)

    def create_tunnel(self, name: str) -> str:
        """Create the tunnel `name`; its id (lowercase UUID), read from the credentials file it was created with: every later
        step names the tunnel by that id, so it is the tunnel these credentials run."""
        result = self.runner(self._tunnel("create", "--credentials-file", str(self.credentials(name)), name))
        if result.returncode != 0 or not self.credentials(name).is_file():
            raise AgentToolError(
                what=f"cannot create the tunnel {name!r}", why=_detail(result), fix="check the Cloudflare login and the network"
            )
        tunnel_id = read_credentials(self.credentials(name)).tunnel_id
        if tunnel_id is None or not TUNNEL_UUID.fullmatch(tunnel_id):
            # Kept, the file would make the next run take it for a stale or old file of this tunnel and refuse wrongly.
            self.credentials(name).unlink()
            raise AgentToolError(
                what=f"the credentials file cloudflared wrote for the new tunnel {name!r} names no valid tunnel id",
                why="DNS is routed by that id, so without it the hostname could reach another tunnel; the file was removed",
                fix=(
                    f"the tunnel now exists on the account: re-run with --tunnel-name {name} to fetch its credentials by id, "
                    "or delete it (Cloudflare dashboard, Zero Trust > Networks > Tunnels) and re-run"
                ),
            )
        return tunnel_id

    def fetch_credentials(self, name: str, tunnel_id: str) -> None:
        """Credentials of a tunnel that exists on the account but not in this directory (created elsewhere, or files lost). By
        id, not name: a tunnel recreated under the name since it was checked is never fetched; the file must carry that id."""
        credentials = self.credentials(name)
        result = self.runner(self._tunnel("token", "--cred-file", str(credentials), tunnel_id))
        if result.returncode != 0 or not credentials.is_file():
            raise AgentToolError(
                what=f"cannot fetch the credentials of the existing tunnel {name!r}",
                why=_detail(result),
                fix="use another --tunnel-name (a tunnel created before cloudflared 2022.3 has no fetchable token)",
            )
        if read_credentials(credentials).tunnel_id != tunnel_id:
            credentials.unlink()
            raise AgentToolError(
                what=f"the credentials fetched for the tunnel {name!r} belong to another tunnel",
                why="their TunnelID is not the id that was checked; they were removed again",
                fix="re-run the command",
            )

    def route_dns(self, name: str, hostname: str, *, tunnel_id: str, overwrite: bool = False) -> Route:
        """Point `hostname` at the tunnel `name` (only named in an error), given by `tunnel_id`, keyword-only so it cannot be
        swapped with the name: cloudflared resolves a name through the credentials-file of its config first, which may be
        another tunnel's. Without `overwrite` an existing record that points elsewhere is left alone (CONFLICT)."""
        result = self.runner(self._tunnel("route", "dns", *(["--overwrite-dns"] if overwrite else []), tunnel_id, hostname))
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
