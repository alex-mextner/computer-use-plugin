# computer

Give a chat client (Claude Desktop today) a shell and files on this PC through MCP (Desktop Commander).

One stdio process per client, no shared daemon and no HTTP gateway. The core (`computer mcp`) owns a pinned Desktop Commander
install; each client gets a thin adapter (`computer claude`) that only knows that client's config.

## Core: `computer mcp`

- `computer mcp install` installs one exact, pinned version of the npm package `@wonderwhy-er/desktop-commander` (the package itself is pinned; its own
  dependencies resolve at install time) into
  `${XDG_DATA_HOME:-~/.local/share}/computer/desktop-commander` and records the absolute `node` path. Idempotent. Needs `node` and
  `npm` (exit 127 without them, 7 on a network failure).
- `computer mcp serve` is what a client's config launches: it replaces itself with the pinned Desktop Commander over stdio, needs
  no `PATH`, and prints only MCP protocol on stdout. Exit 5 with a fix message if `computer mcp install` has not been run.

## Claude Desktop: `computer claude`

- `computer claude install` sets `mcpServers["desktop-commander"]` in `${XDG_CONFIG_HOME:-~/.config}/Claude/claude_desktop_config.json`
  to the absolute path of this `computer` executable with `mcp serve`, runs `computer mcp install`, and makes sure
  `~/.config/autostart/claude-desktop.desktop` exists and is enabled. Every other key of the config is preserved, the write is
  atomic, a timestamped backup is made first, and invalid JSON is refused (exit 2) without touching the file. Claude Desktop is
  never restarted by this tool: restart it yourself to pick the change up.
- `computer claude status` checks the config entry, the executable, the autostart entry, and performs a real MCP handshake
  (`initialize`, `tools/list`) against the configured command under a minimal environment. Exit 0 healthy, 3 unhealthy.
- `computer claude uninstall` removes only the `desktop-commander` entry (after a backup); the managed install and the autostart
  entry stay.

Design: `docs/superpowers/specs/2026-11-11-claude-desktop-support-design.md`.

Private repository. License: MIT.
