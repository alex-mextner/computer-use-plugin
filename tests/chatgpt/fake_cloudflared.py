"""A stand-in for the pinned `cloudflared` CLI: the subcommands `computer chatgpt` drives, with the real flag shapes and messages.

It is installed as the "downloaded" binary (`stand_in_for_the_network(binaries={"cloudflared": FAKE_CLOUDFLARED})`), so the real
command code runs against it. The account (tunnels with their ids and edge connections, DNS records) lives in $FAKE_CF_STATE;
every call is logged there with its HOME and PATH, so a test can prove that ~/.cloudflared was never used and that the login could
not start a browser by itself. Messages are the real ones of cloudflared 2026.10.7 (`route dns` success/"already
configured"/"already exists"); JSON outputs have the real shapes: `tunnel list` without connections (Cloudflare dropped the
field on 2026-10-05), `tunnel info` with its connectors and their connections (Cloudflare's connections endpoint).
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
def tunnel_id(name):
    return ids.get(name, "00000000-0000-0000-0000-0000000000aa")
def resolve(ref):
    # cloudflared takes an id or a name wherever it takes a tunnel.
    found = [n for n in tunnels if ref in (n, tunnel_id(n))]
    if len(found) != 1:
        fail("Tunnel not found" if not found else "ambiguous tunnel " + ref)
    return found[0]
def write_credentials(path, name):
    Path(path).write_text(json.dumps({"AccountTag": "acct", "TunnelSecret": "c2VjcmV0", "TunnelID": tunnel_id(name)}))
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
    print(json.dumps(info))
elif command == "create":
    name = rest[-1]
    if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}", name):
        fail("tunnel name cannot be a UUID")
    if name in tunnels:
        fail("failed to create tunnel: Create Tunnel API call failed: tunnel with name already exists")
    write_credentials(rest[rest.index("--credentials-file") + 1], name)
    save("tunnels.json", tunnels + [name])
    print("Created tunnel " + name + " with id 00000000-0000-0000-0000-0000000000aa")
elif command == "token":
    write_credentials(rest[rest.index("--cred-file") + 1], resolve(rest[-1]))
elif command == "route" and rest[1] == "dns":
    words = [w for w in rest[2:] if not w.startswith("--")]
    name, host = words
    routes = load("routes.json", {})
    if routes.get(host) == name:
        print(host + " is already configured to route to your tunnel tunnelID=00000000-0000-0000-0000-0000000000aa")
    # cloudflared reads TUNNEL_FORCE_PROVISIONING_DNS as --overwrite-dns
    elif host in routes and "--overwrite-dns" not in rest and os.environ.get("TUNNEL_FORCE_PROVISIONING_DNS") != "true":
        fail(
            "Failed to add route: code: 1003, reason: Failed to create record " + host
            + " with err An A, AAAA, or CNAME record with that host already exists."
        )
    else:
        routes[host] = name
        save("routes.json", routes)
        print("Added CNAME " + host + " which will route to this tunnel tunnelID=00000000-0000-0000-0000-0000000000aa")
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
