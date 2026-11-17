# Changelog

## 0.2.0

- ChatGPT target in one command: `computer chatgpt install --hostname <name on a Cloudflare zone>` checks the prerequisites (node,
  npm, loginctl, a systemd user manager, the zone on Cloudflare, free loopback ports) before writing anything, installs the shared
  Desktop Commander plus pinned, sha256-verified mcp-auth-proxy v2.10.2 and cloudflared 2026.10.7, generates the login password
  (0600, never printed), logs in to Cloudflare when needed (the only interactive step), creates the named tunnel, routes the DNS
  name (asks before replacing an existing record; `--yes`), writes and starts systemd user units (`Restart=always`,
  `StartLimitIntervalSec=0`, lingering), waits for the public URL to answer 401 with the OAuth challenge and prints the exact ChatGPT
  connector fields. Idempotent; units whose file changed are restarted.
- The building blocks and day-2 commands of the earlier, never-merged ChatGPT work are re-homed on the shared command tree: `init`,
  `login`, `start`, `stop`, `restart`, `end`, `status`, `logs`, `doctor`, `connect`, `run`, `uninstall`, internal `shim` and `quiet`,
  plus the experimental OpenAI tunnel-client mode (`init --mode tunnel`).
- Desktop Commander for ChatGPT comes from the shared core: mcp-auth-proxy's (and tunnel-client's) stdio child is the absolute
  `computer mcp serve`; there is no second install and no `npx @latest`.
- cloudflared state (certificate, tunnel credentials, config.yml) lives in the target's own directory
  (`~/.config/computer/chatgpt/cloudflared`, `--cloudflared-dir`); `~/.cloudflared` is never used unless pointed at.
- The release-binary pins moved into the one pin table, `computerlib/pins.py`.
- README: two targets with a per-target dependency table (including the ChatGPT plan requirement), the install URL of
  `computer-use-plugin`, and the HTTP gateway the ChatGPT target needs.
- Claude Desktop support. `computer mcp install` puts a pinned Desktop Commander (0.3.2) in a managed directory and `computer mcp serve`
  launches it over stdio and needs no `PATH`. `computer claude install|status|uninstall` wires it into Claude Desktop's config
  (atomic write, backup, other keys preserved), ensures the autostart entry, and checks everything with a real MCP handshake.
