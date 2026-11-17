"""The `tunnel` mode: OpenAI's `tunnel-client` runs Desktop Commander over stdio and relays ChatGPT's requests through OpenAI's
own tunnel service. EXPERIMENTAL: no tool call from ChatGPT has gone through it on this machine (see README); what is proven is
the daemon plus Desktop Commander's stdio child against a stand-in control plane (tests/chatgpt/test_tunnel_end_to_end.py).

WHAT: the process definition (argv), the runtime-key file and the tunnel-id check. Nothing here is public: there is no proxy,
shim, OAuth or Cloudflare in this mode.
INVARIANTS: the runtime API key (restricted to Tunnels Read+Use) lives in a 0600 file and reaches the daemon as `file:<path>`,
never in an argument list, the environment or a log; the daemon's health/admin port is loopback only and defaults to 8794 so it
never collides with the guardian tunnel (8793); exactly ONE daemon may run per tunnel id (stdio limit), so the id is never shared
with another unit.
"""

from __future__ import annotations

import re
from pathlib import Path

from agenttools_errors import UsageError

from computerlib.targets.chatgpt.privatefile import write_private
from computerlib.targets.chatgpt.stack import Process

TUNNEL_ID = re.compile(r"^tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}$")
KEY_FILE_MODE = 0o600


def valid_tunnel_id(value: str) -> bool:
    return TUNNEL_ID.match(value) is not None


def require_tunnel_id(tunnel_id: str) -> str:
    """The configured tunnel id, or a usage error that says where to get one (the id is not secret; the key is)."""
    if not tunnel_id:
        raise UsageError(
            what="no tunnel id",
            why="tunnel mode serves one OpenAI tunnel, and none is configured",
            fix="create a tunnel in the OpenAI Platform, then run `computer chatgpt init --mode tunnel --tunnel-id <tunnel_...>`",
        )
    if not valid_tunnel_id(tunnel_id):
        raise UsageError(
            what="the tunnel id is not valid",
            why="it looks like `tunnel_` followed by 32 lowercase letters or digits (optionally `tunnel_xxxx_` + 32)",
            fix="copy the id of the tunnel from the OpenAI Platform and pass it with --tunnel-id",
        )
    return tunnel_id


def write_key(path: Path, key: str) -> None:
    """Store the runtime key (0600 even over an existing wider file, in a 0700 directory); the value is never echoed."""
    write_private(path, key.strip() + "\n")


def has_key(path: Path) -> bool:
    try:
        return bool(path.read_text().strip())
    except OSError:
        return False


def mcp_command_value(command: tuple[str, ...]) -> str:
    """The `--mcp.command` value: tunnel-client takes ONE string (`command=...,channel=...` form) and splits it on whitespace, so
    a word with whitespace or a comma cannot be expressed; refuse it instead of starting a daemon that would run something
    else."""
    bad = [word for word in command if not word or any(c.isspace() or c == "," for c in word)]
    if bad:
        raise UsageError(
            what=f"cannot hand {bad[0]!r} to tunnel-client",
            why="tunnel-client splits its --mcp.command on whitespace and commas",
            fix="install `computer` under a path without spaces or commas (for example `uv tool install`, ~/.local/bin)",
        )
    return " ".join(command)


def tunnel_client(
    binary: Path, tunnel_id: str, key_file: Path, mcp_command: tuple[str, ...], health_port: int, env: tuple[tuple[str, str], ...]
) -> Process:
    return Process(
        name="tunnel-client",
        description="OpenAI tunnel-client relaying ChatGPT to Desktop Commander (stdio), experimental",
        argv=(
            str(binary),
            "run",
            "--control-plane.tunnel-id",
            tunnel_id,
            "--control-plane.api-key",
            f"file:{key_file}",
            "--mcp.command",
            mcp_command_value(mcp_command),
            "--health.listen-addr",
            f"127.0.0.1:{health_port}",
        ),
        env=env,
        restart_seconds=5,
    )
