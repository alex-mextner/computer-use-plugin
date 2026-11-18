# Changelog

## 0.2.2

- `computer mcp serve` starts Desktop Commander with `--no-onboarding` (#21). In a HOME where Desktop Commander had never run (any
  new machine), its `initialize` answer waited for its welcome page (a feature-flag fetch, then, for the A/B arm that gets the
  page, currently everyone, `xdg-open https://desktopcommander.app/welcome/`). In a desktop session where that started a new
  browser, xdg-open waited until the browser closed, so `initialize` was never answered and the ChatGPT stack's mcp-auth-proxy
  exited. The welcome page is no longer opened.

## 0.2.1

- `computer mcp install` installs Desktop Commander 0.3.2 with its whole dependency tree locked: `npm ci --ignore-scripts` from a
  bundled `package-lock.json` instead of `npm install` of the top-level package. Fixes the install failing on npm's 404 for
  puppeteer-core 25.18.1 (the lock holds 25.18.0). Each lock is installed into its own tree (`trees/<lock sha256>`) in a staging
  directory and switched to only when complete, so a failed install leaves the working one serving. Older trees (and the
  `node_modules` of a 0.2.0 install) are left in place, since a client started from one may still run; delete them by hand after
  restarting clients. An existing install is redone once this way.
- Install scripts stay off, except puppeteer's browser download, which runs on its own afterwards (as the old `npm install` did)
  so PDF output has a browser; if that download fails, install warns with the command to re-run and still succeeds. A tree is
  activated only when the ripgrep binary for node's own platform is present (npm ci is run hoisted, with optional packages and
  with `--os`/`--cpu` of the recorded node, whatever the npmrc or `npm_config_*` say).

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
