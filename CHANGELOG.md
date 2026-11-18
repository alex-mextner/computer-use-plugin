# Changelog

## 0.3.0

- Several machines, one command each: `computer chatgpt install --domain <zone>` serves this machine at
  `https://<machine>-mcp.<zone>/mcp` through a Cloudflare tunnel named after the machine. The machine name is the short system
  hostname as a DNS label (lowercase, `[a-z0-9-]`, at most 59 characters so `<machine>-mcp` is one label; one that reads as a
  UUID is refused, since cloudflared refuses it as a tunnel name); `--machine-name` chooses it. One label below the zone because
  Cloudflare's free Universal SSL covers only first-level subdomains; a `--domain`
  that is not the zone itself is refused. `--hostname` still sets the full name (and clears a saved domain); configurations made
  with `--hostname` keep working unchanged. `domain` and `machine_name` are saved in `config.toml`, so re-runs need no flags.
- The default tunnel name is the machine name (was `computer-<hostname with dashes>`; a saved tunnel name is kept). A tunnel of
  that name that exists on the account without credentials here (another machine of the same name, a lost install) is refused
  with exit 2 instead of having its credentials fetched; naming it with `--tunnel-name` adopts it (an empty `--tunnel-name` is
  refused, never taken for the saved name). A changed `--machine-name` moves the tunnel only when the hostname is derived from
  `--domain`: with a fixed `--hostname` the tunnel serving it is kept, so its DNS record is never re-pointed.
- `--cloudflare-cert <path>` (`install`, `login`) reuses the account certificate of an earlier `cloudflared tunnel login`, copied
  mode 600, so a second machine needs no browser; the file is read once (`<(ssh … cat cert.pem)` works) and one that is not such
  a certificate (as cloudflared reads it: one token block on its own lines, no unknown block) is refused before anything is
  written, and a different certificate already in place is refused. On a filesystem without hard links a complete copy is
  renamed into place under a lock instead, so no reader ever sees a partial certificate.
- `install` saves `config.toml` and writes the unit files only after the tunnel and DNS step succeeded: a run that stops there (a
  refused tunnel name, a DNS record kept) leaves both as they were, so `connect`, `doctor` and `uninstall` still describe the
  running stack and the corrected re-run restarts the running services for the new name. `uninstall --purge` also runs without
  a config, to clean up after a first install that stopped there. The refusal and `doctor` suggest `--machine-name` only where
  it moves the tunnel (a `--domain` setup), otherwise `--tunnel-name <name>` for this machine's own tunnel.
- The Cloudflare login URL is printed on its own line and flushed, and the browser is only started when there is a display: on a
  headless machine (ssh) open the URL on any device and the command continues by itself.
- `connect` and `install` suggest the machine name as the ChatGPT connector name (was `Computer`), so connectors stay apart.
- A CPU without pinned builds is refused before anything is written, naming the covered ones (x86_64, aarch64; mcp-auth-proxy has
  no 32-bit ARM build). macOS stays out of scope (no systemd).

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
