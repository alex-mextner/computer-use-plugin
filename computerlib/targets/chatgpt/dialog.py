"""What the user types into ChatGPT's "Add custom MCP server" dialog, and what the known failures mean.

ChatGPT has no URL scheme, API or CLI to create a custom MCP server (checked 2026-11-08: two open feature requests, no prefill
link), so the CLI can only show every field with its value and reason, and then verify from the server side. If OpenAI ever ships
a prefill link only `prefill_url` changes. Labels are the dialog's own wording.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

SETTINGS_PAGE = "https://chatgpt.com/plugins"
PERSONAL_PAGE = "https://chatgpt.com/plugins?view=personal"
SMOKE_PROMPT = 'ask it to run `start_process` with `command="printf RDC_CONNECTION_OK"`.'
# OpenAI Help Center "Connecting custom MCP servers in ChatGPT" (checked 2026-11-17): full MCP (write tools, which Desktop
# Commander needs) is for Business and Enterprise/Edu on ChatGPT web; Pro gets read/fetch only. Keep README's dependency table in
# step.
PLAN_NOTE = (
    "ChatGPT on the web, in a plan whose workspace lets you add custom MCP servers with write tools: Business or Enterprise/Edu "
    "(owner/admin, or a member granted 'Create plugins with MCPs'); OpenAI documents Pro as read/fetch only"
)


@dataclass(frozen=True)
class Field:
    label: str
    value: str
    why: str


def prefill_url(server_url: str) -> str | None:
    """A link that opens the dialog filled in; ChatGPT offers none, so there is nothing to return."""
    del server_url
    return None


def public_fields(server_url: str, name: str = "Computer", host: str | None = None) -> list[Field]:
    machine = host or socket.gethostname()
    return [
        Field("Icon (optional)", "skip", "cosmetic only"),
        Field("Name", name, "the name you @-mention in a chat; one per machine keeps several machines apart"),
        Field(
            "Description (optional)", f"Files and terminal on {machine}", "helps ChatGPT pick the tool; shown in the plugin list"
        ),
        Field("Connection", "Server URL", 'not "Tunnel": this PC is reached through a public hostname'),
        Field("Server URL", server_url, "the MCP endpoint, including /mcp (copied to the clipboard)"),
        Field("Authentication", "OAuth", "the proxy runs OAuth 2.1 with dynamic client registration"),
        Field("Advanced OAuth settings", "leave closed", "ChatGPT discovers them from the server"),
        Field("I understand and want to continue", "tick it", "required: this server can run commands on this PC"),
        Field("Button", "Create as a plugin", "then install the plugin and start a NEW chat"),
    ]


def tunnel_fields(tunnel_id: str, name: str = "Computer", host: str | None = None) -> list[Field]:
    machine = host or socket.gethostname()
    return [
        Field("Icon (optional)", "skip", "cosmetic only"),
        Field("Name", name, "the name you @-mention in a chat; one per machine keeps several machines apart"),
        Field(
            "Description (optional)", f"Files and terminal on {machine}", "helps ChatGPT pick the tool; shown in the plugin list"
        ),
        Field("Connection", "Tunnel", 'not "Server URL": nothing is public, OpenAI relays the requests to this PC'),
        Field(
            "Tunnel",
            tunnel_id,
            'pick it under "Available tunnels"; if it is absent choose "Enter tunnel ID instead" and paste this id',
        ),
        Field(
            "Authentication",
            "None (No authentication)",
            "the tunnel is the access boundary: anyone in your OpenAI org/ChatGPT workspace with Tunnels Use can attach it; "
            "NOT verified here: whether ChatGPT offers None for a tunnel connector",
        ),
        Field("Advanced OAuth settings", "leave closed", "only for OAuth"),
        Field("I understand and want to continue", "tick it", "required: this server can run commands on this PC"),
        Field("Request budget", "120 s per request", "OpenAI's hosted deadline: a tool call that takes longer fails"),
        Field(
            "One daemon per tunnel", "this PC only", "never start a second tunnel-client (or another unit) for the same tunnel id"
        ),
        Field("Button", "Create as a plugin", "then install the plugin and start a NEW chat"),
    ]


def tunnel_steps() -> list[str]:
    return [
        "Keep `computer chatgpt` running: discovery needs the daemon (the checklist below watches it).",
        f"Open {SETTINGS_PAGE} in a signed-in ChatGPT web session (web only, not mobile).",
        'Click "+" (plus) -> "Add custom MCP server".',
        "Fill the fields below, tick the risk checkbox, click Create as a plugin.",
        f"Install the plugin ({PERSONAL_PAGE}), open a new chat, type @ and pick it, and {SMOKE_PROMPT}",
    ]


def steps() -> list[str]:
    return [
        f"Open {SETTINGS_PAGE} in a signed-in ChatGPT web session (web only, not mobile).",
        'Click "+" (plus) -> "Add custom MCP server".',
        "Fill the fields below, tick the risk checkbox, click Create as a plugin.",
        "ChatGPT then sends you to the login page of this PC: enter the password (it is in the file named below, never printed).",
        f"Install the plugin ({PERSONAL_PAGE}), open a new chat, type @ and pick it.",
    ]


FAILURES: tuple[tuple[str, str], ...] = (
    (
        "Couldn't discover OAuth settings. Check the server URL or enter settings manually",
        (
            "the URL does not answer 401 with a WWW-Authenticate resource_metadata challenge: run `computer chatgpt doctor`; "
            "also check the hostname, the tunnel and that the path ends in /mcp"
        ),
    ),
    (
        "This app does not provide a browser setup URL right now",
        (
            "turn on multi-factor authentication in ChatGPT (Settings -> Security) and retry; "
            "otherwise run `computer chatgpt doctor`"
        ),
    ),
    ("Couldn't create MCP app, try again.", "retry; nothing reached this PC, so it is on ChatGPT's side"),
    ("invalid_client later on", "the OAuth state of this PC was wiped (`end --revoke`): create the connector again"),
    (
        "the login page says the password is wrong",
        "the password is the PASSWORD value in the file shown by `computer chatgpt connect`",
    ),
)

TUNNEL_FAILURES: tuple[tuple[str, str], ...] = (
    (
        "the tunnel is not in the Available tunnels list",
        (
            "not associated with your ChatGPT workspace, the key lacks Tunnels Use, or it is under 30 s old: "
            'use "Enter tunnel ID instead"'
        ),
    ),
    (
        "discovery or tool calls fail",
        (
            "tunnel-client is not running/ready, the tunnel id is wrong, the key lacks Tunnels Read+Use, "
            "or a call took over 120 s: "
            "run `computer chatgpt doctor`"
        ),
    ),
    ("Couldn't create MCP app, try again.", "retry; if the checklist below shows nothing arriving, it failed on ChatGPT's side"),
)
