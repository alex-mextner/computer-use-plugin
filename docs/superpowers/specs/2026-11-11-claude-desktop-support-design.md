# Claude Desktop support: design

## Goal

Claude Desktop on Linux must start Desktop Commander (DC, the npm package `@wonderwhy-er/desktop-commander`) by itself on every
login, with the bare `PATH` an autostarted desktop app has, and without depending on a floating `npx` cache. The core must stay
client-neutral so a later ChatGPT target can reuse it.

## Architecture

Shared core plus one adapter per client; one stdio process per client. No shared daemon, no HTTP gateway, no `mcp-remote`.

- Core target `computer mcp`: owns the pinned DC install and the stdio launcher.
- Adapter target `computer claude`: owns only what is specific to Claude Desktop (its config file, its autostart entry).

Claude Desktop spawns `computer mcp serve` (absolute path to the `computer` executable, recorded in its config), and `serve`
replaces itself with the pinned `node <dc>/dist/index.js`, so stdout is the pure MCP protocol stream.

## Components

- `computerlib/pins.py`: the single pin table (DC package and exact version, currently `0.3.2`, the version in the local npx cache
  and also the latest stable on npm when pinned).
- `computerlib/desktop_commander.py`: managed install dir `${XDG_DATA_HOME:-~/.local/share}/computer/desktop-commander`,
  `install.json` record (pinned version, absolute `node` path), `install()`, `launch_spec()`.
- `computerlib/mcp_probe.py`: real MCP handshake (`initialize`, `notifications/initialized`, `tools/list`) against a spawned
  command, time-bounded, child always killed.
- `computer mcp install`: `npm install --prefix <dir> --save-exact <pkg>@<pin>`; idempotent; records `node`.
- `computer mcp serve`: reads `install.json`, `os.execv(node, [node, entry])`. Needs no `PATH`.
- `computer claude install`: sets `mcpServers["desktop-commander"]` in `${XDG_CONFIG_HOME:-~/.config}/Claude/claude_desktop_config.json`,
  runs the core install, ensures `~/.config/autostart/claude-desktop.desktop` exists and is enabled.
- `computer claude status`: config entry, executable, real handshake under a minimal environment, autostart, Claude running (info).
- `computer claude uninstall`: removes only the `desktop-commander` entry.

The entry name `desktop-commander` keeps tool namespacing identical to earlier hand-written configs. DC's own state in
`~/.claude-server-commander/` is never touched.

## Notes

- Only the top-level package is pinned; its npm dependencies resolve when `install` runs (a lockfile is a possible later hardening).
- The recorded `node` is a stable symlink path where one exists, the real binary behind a version-manager shim otherwise, and must be
  Node 18 or newer. A custom `XDG_DATA_HOME` at install time is carried in the config entry's `env`, because Claude autostarts without
  the user's shell variables. An existing entry keeps the user's own extra keys.

## Error handling

Exit codes follow `--help`: missing `node`/`npm` 127, network failure 7, missing install or executable 5, invalid JSON in the
Claude config 2 (the file is never overwritten), unhealthy `status` 3. Config writes are atomic (temp file in the same directory,
then rename), keep the file mode, and are preceded by a timestamped backup when the content changes. Claude Desktop is never
restarted or killed; the command says it must be restarted to pick the change up.

## Testing

Hermetic pytest with fakes: fake `node`/`npm` scripts on a temp `PATH`, a stub stdio MCP server for the handshake, `serve` run
as a real subprocess under `env -i` to prove the minimal-environment case. Nothing touches the real home or the network.
