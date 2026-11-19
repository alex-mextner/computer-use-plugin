"""A stand-in for the pinned `cloudflared` CLI: the subcommands `computer chatgpt` drives, with the real flag shapes and messages.

It is installed as the "downloaded" binary (`stand_in_for_the_network(binaries={"cloudflared": FAKE_CLOUDFLARED})`), so the real
command code runs against it. The account (tunnels with their ids and edge connections, DNS records) lives in $FAKE_CF_STATE;
every call is logged there with its HOME and PATH, so a test can prove that ~/.cloudflared was never used and that the login could
not start a browser by itself. Messages are the real ones of cloudflared 2026.10.7 (`route dns` success/"already
configured"/"already exists"); JSON outputs have the real shapes: `tunnel list` without connections (Cloudflare dropped the
field on 2026-10-05), `tunnel info` with its connectors and their connections (Cloudflare's connections endpoint). Like
cloudflared's findID, a tunnel given by name resolves through the TunnelID of a `credentials-file` the --config names before the
account is asked, so a test can show which tunnel a name really reached.
"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_BODY = """
import json, os, re, sys
from pathlib import Path
state = Path(os.environ["FAKE_CF_STATE"])
state.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
with (state / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": args, "home": os.environ.get("HOME", ""), "path": os.environ.get("PATH")}) + "\\n")
def load(name, default):
    p = state / name
    return json.loads(p.read_text()) if p.exists() else default
def save(name, value):
    (state / name).write_text(json.dumps(value))
def fail(message):
    sys.stderr.write(message + "\\n")
    sys.exit(1)
assert args[0] == "tunnel", args
rest = args[1:]
options = {}
while rest and rest[0].startswith("--"):
    if rest[0] in ("--config", "--origincert", "--metrics"):
        options[rest[0]] = rest[1]
        rest = rest[2:]
    else:
        rest = rest[1:]
command = rest[0] if rest else ""
if command == "login":
    home = Path(os.environ["HOME"])
    cert = home / ".cloudflared" / "cert.pem"
    if cert.exists():
        fail("You have an existing certificate at " + str(cert) + " which login would overwrite.")
    print("Please open the following URL and log in with your Cloudflare account:")
    print("")
    print("https://dash.cloudflare.com/argotunnel?aud=&callback=https%3A%2F%2Flogin.cloudflareaccess.org%2Ffake")
    print("")
    print("Leave cloudflared running to download the cert automatically.")
    if os.environ.get("FAKE_CF_LOGIN") == "fail":
        fail("Failed to write the certificate: login timed out")
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_text("fake account certificate\\n")
    print("You have successfully logged in.")
    sys.exit(0)
origin = options.get("--origincert", os.path.join(os.environ.get("HOME", ""), ".cloudflared", "cert.pem"))
if command in ("list", "create", "token", "route") and not os.path.isfile(origin):
    fail("Cannot determine default origin certificate path. originCertPath=" + origin)
if "--config" in options and not os.path.isfile(options["--config"]):
    fail("Cannot read config file " + options["--config"])
tunnels = load("tunnels.json", [])
ids = load("ids.json", {})
live = load("connections.json", {})
UUID = r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}"
def nth_id(n):
    return "00000000-0000-0000-0000-0000000000" + format(0xAA + n, "02x")
def tunnel_id(name):
    # ids.json holds the id `create` gave a tunnel (the first unused of ...aa, ...ab, ...); one a test listed directly
    # gets the id of its position, so every tunnel of the account has its own.
    return ids.get(name) or nth_id(tunnels.index(name) if name in tunnels else len(tunnels))
def configured_tunnel_id():
    # The TunnelID of the credentials-file the --config names, as cloudflared's findID reads it before asking the account.
    config = options.get("--config")
    if not config:
        return None
    for line in Path(config).read_text().splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "credentials-file":
            try:
                return json.loads(Path(value.strip().strip("'").strip('"')).read_text()).get("TunnelID") or None
            except (OSError, ValueError):
                return None
    return None
def resolve(ref):
    # cloudflared takes an id or a name wherever it takes a tunnel; a name is first looked up in the credentials file the
    # config names (its TunnelID wins, whichever tunnel that is), and only then on the account.
    if not re.fullmatch(UUID, ref):
        ref = configured_tunnel_id() or ref
    found = [n for n in tunnels if ref in (n, tunnel_id(n))]
    if len(found) != 1:
        fail("Tunnel not found" if not found else "ambiguous tunnel " + ref)
    return found[0]
def write_credentials(path, name):
    fields = {"AccountTag": "acct", "TunnelSecret": "c2VjcmV0", "TunnelID": tunnel_id(name)}
    if os.environ.get("FAKE_CF_CREDENTIALS") == "no-tunnel-id":  # the shape cloudflared wrote before TunnelID existed
        del fields["TunnelID"]
    Path(path).write_text(json.dumps(fields))
    os.chmod(path, 0o400)
if command == "list":
    name = rest[rest.index("--name") + 1]
    print(json.dumps([{"id": tunnel_id(n), "name": n, "created_at": "2026-11-01T00:00:00Z"} for n in tunnels if n == name]))
elif command == "info":
    name = resolve(rest[-1])
    info = {"id": tunnel_id(name), "name": name, "createdAt": "2026-11-01T00:00:00Z"}
    counts = live.get(name, {"active": 0, "pending": 0})
    if counts is not None:  # None: an answer without connectors (the unknown case)
        conns = [{"colo_name": "fra08", "is_pending_reconnect": False}] * counts["active"]
        conns += [{"colo_name": "fra08", "is_pending_reconnect": True}] * counts["pending"]
        info["conns"] = [{"id": "11111111-1111-1111-1111-111111111111", "version": "2026.10.7", "conns": conns}] if conns else []
        if counts.get("blind"):  # a connector listed without its own connections: null, or no field at all
            connector = {"id": "22222222-2222-2222-2222-222222222222", "version": "2026.10.7"}
            if counts["blind"] == "null":
                connector["conns"] = None
            info["conns"].append(connector)
    print(json.dumps(info))
elif command == "create":
    name = rest[-1]
    if re.fullmatch(UUID, name):
        fail("tunnel name cannot be a UUID")
    if name in tunnels:
        fail("failed to create tunnel: Create Tunnel API call failed: tunnel with name already exists")
    used = {tunnel_id(n) for n in tunnels}
    ids[name] = next(nth_id(n) for n in range(len(tunnels) + 1) if nth_id(n) not in used)
    save("ids.json", ids)
    write_credentials(rest[rest.index("--credentials-file") + 1], name)
    save("tunnels.json", tunnels + [name])
    print("Created tunnel " + name + " with id " + tunnel_id(name))
elif command == "token":
    write_credentials(rest[rest.index("--cred-file") + 1], resolve(rest[-1]))
elif command == "route" and rest[1] == "dns":
    words = [w for w in rest[2:] if not w.startswith("--")]
    ref, host = words
    name = resolve(ref)
    routes = load("routes.json", {})
    if routes.get(host) == name:
        print(host + " is already configured to route to your tunnel tunnelID=" + tunnel_id(name))
    # cloudflared reads TUNNEL_FORCE_PROVISIONING_DNS as --overwrite-dns
    elif host in routes and "--overwrite-dns" not in rest and os.environ.get("TUNNEL_FORCE_PROVISIONING_DNS") != "true":
        fail(
            "Failed to add route: code: 1003, reason: Failed to create record " + host
            + " with err An A, AAAA, or CNAME record with that host already exists."
        )
    else:
        routes[host] = name
        save("routes.json", routes)
        print("Added CNAME " + host + " which will route to this tunnel tunnelID=" + tunnel_id(name))
else:
    fail("unexpected call: " + " ".join(args))
"""
# Absolute interpreter: the login runs with an empty PATH, where `#!/usr/bin/env python3` would not start.
FAKE_CLOUDFLARED = f"#!{sys.executable}\n{_BODY}".encode()


@dataclass(frozen=True)
class Call:
    argv: list[str]
    home: str
    path: str | None


def calls(state: Path) -> list[Call]:
    log = state / "calls.jsonl"
    if not log.exists():
        return []
    found: list[Call] = []
    for line in log.read_text().splitlines():
        raw = cast("dict[str, Any]", json.loads(line))
        found.append(Call([str(word) for word in raw["argv"]], str(raw["home"]), raw["path"]))
    return found


def routes(state: Path) -> dict[str, str]:
    path = state / "routes.json"
    return json.loads(path.read_text()) if path.exists() else {}


def tunnels(state: Path) -> list[str]:
    path = state / "tunnels.json"
    return json.loads(path.read_text()) if path.exists() else []


def existing_record(state: Path, host: str, points_at: str) -> None:
    """A DNS record that already exists in the zone (another tunnel, or anything that is not ours)."""
    state.mkdir(parents=True, exist_ok=True)
    (state / "routes.json").write_text(json.dumps({**routes(state), host: points_at}))


def _set(state: Path, file: str, key: str, value: object) -> None:
    state.mkdir(parents=True, exist_ok=True)
    path = state / file
    current = cast("dict[str, object]", json.loads(path.read_text())) if path.exists() else {}
    path.write_text(json.dumps({**current, key: value}))


def connected(state: Path, name: str, active: int, pending: int = 0) -> None:
    """The tunnel `name` has `active` open edge connections (a cloudflared runs it right now, here or on another machine) and
    `pending` recently closed ones that Cloudflare still lists as pending reconnect."""
    _set(state, "connections.json", name, {"active": active, "pending": pending})


def connections_unreported(state: Path, name: str) -> None:
    """`tunnel info` answers for `name` without its connectors: whether it runs is unknown."""
    _set(state, "connections.json", name, None)


def connector_without_connections(state: Path, name: str, conns: str, active: int = 0) -> None:
    """`tunnel info` lists, besides a connector with `active` open connections (when above 0), one for `name` whose own
    connections are `conns`: "null" or "missing" (no field). That connector may be running: with no active connection
    shown, whether the tunnel runs is unknown."""
    _set(state, "connections.json", name, {"active": active, "pending": 0, "blind": conns})


def recreated(state: Path, name: str, tunnel_id: str) -> None:
    """The tunnel `name` was deleted and created again (by another machine): same name, new id."""
    _set(state, "ids.json", name, tunnel_id)
    if name not in tunnels(state):
        (state / "tunnels.json").write_text(json.dumps([*tunnels(state), name]))


def account_cert(account: str) -> str:
    """A certificate shaped like the one `cloudflared tunnel login` writes (made-up values), for `account`."""
    token = {"zoneID": f"zone-{account}", "accountID": f"account-{account}", "apiToken": f"token-{account}"}
    body = base64.b64encode(json.dumps(token).encode()).decode()
    return f"-----BEGIN ARGO TUNNEL TOKEN-----\n{body}\n-----END ARGO TUNNEL TOKEN-----\n"
