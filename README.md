# computer

Give a chat client (Claude Desktop or ChatGPT) a shell and files on this PC through MCP (Desktop Commander).

Two targets, each set up by ONE command:

| Target | One command | Needs on this PC / account |
|---|---|---|
| **Claude Desktop** | `computer claude install` | Linux; the Claude Desktop app; Node.js 18+ and npm. For PDF output (write_pdf) on Ubuntu 23.10+: an AppArmor profile for Desktop Commander's Chrome (see [PDF output on Ubuntu 23.10+](#pdf-output-on-ubuntu-2310-apparmor)) |
| **ChatGPT** | `computer chatgpt install --domain example.com` (one per machine: `<machine>-mcp.example.com`) | Linux (x86_64 or aarch64) with systemd `--user` (and `loginctl`); Node.js 18+ and npm; a Cloudflare account with your domain (zone) on it; a ChatGPT plan that lets you add custom MCP servers with write tools (see below). `cloudflared` and `mcp-auth-proxy` are downloaded automatically, pinned and sha256-verified. For PDF output (write_pdf) on Ubuntu 23.10+: an AppArmor profile for Desktop Commander's Chrome (see [PDF output on Ubuntu 23.10+](#pdf-output-on-ubuntu-2310-apparmor)). macOS is not supported (no systemd) |

Both run the same pinned [Desktop Commander](https://github.com/wonderwhy-er/DesktopCommanderMCP) from the shared core
(`computer mcp`), never a floating `npx ...@latest`.

**ChatGPT plan.** ChatGPT reaches custom MCP servers only from ChatGPT on the web, over a public HTTPS URL (or OpenAI's Secure MCP
Tunnel, experimental here). Per OpenAI's help center
([Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt),
checked 2026-11-17), custom MCP servers with full MCP (write actions, which running commands and editing files are) are available on
**Business** and **Enterprise/Edu** workspaces: owners/admins, or members an Enterprise/Edu admin granted *Create plugins with MCPs*;
**Pro** is documented as read/fetch only. The connector is added under [Plugins](https://chatgpt.com/plugins) -> "+" -> *Add custom MCP
server* ([OpenAI developer docs](https://developers.openai.com/api/docs/guides/custom-mcp-server)); no separate Developer Mode switch is
needed in the current Plugins experience.

## Install the tool

```sh
uv tool install "hyper-computer @ git+https://github.com/alex-mextner/computer-use-plugin.git"
```

Needs Python 3.13. License: MIT.

## Architecture

```
Claude Desktop --stdio--> computer mcp serve --> Desktop Commander (pinned)

ChatGPT --HTTPS--> Cloudflare named tunnel --> shim (127.0.0.1:8767) --> mcp-auth-proxy (127.0.0.1:8766) --stdio--> computer mcp serve
```

The core (`computer mcp`) owns one pinned Desktop Commander install; each client gets a thin adapter that only knows that client.
Claude Desktop starts the stdio process itself: no daemon, no HTTP. ChatGPT can only reach a public HTTPS URL, so its target runs
an always-on HTTP gateway as systemd user units: a Cloudflare named tunnel (no open port on this PC), an OAuth 2.1 proxy with a password
login in front of Desktop Commander, and a small shim that adds what ChatGPT's connector setup needs.

## Core: `computer mcp`

- `computer mcp install` installs one exact, pinned version of the npm package `@wonderwhy-er/desktop-commander` together with its
  whole dependency tree, locked: `npm ci --ignore-scripts` from the `package-lock.json` bundled in this package (every package at a
  fixed version with its sha512 integrity), so a broken or re-published transitive package on npm cannot break or change the
  install. It goes into `${XDG_DATA_HOME:-~/.local/share}/computer/desktop-commander` and records the absolute `node` path.
  Idempotent (npm runs again only when the pinned version or the bundled lock changed). Needs `node` and `npm` (exit 127 without
  them, 7 on a network failure).
- `computer mcp serve` is what a client launches: it runs the pinned Desktop Commander over stdio, needs no `PATH`, prints only
  MCP protocol on stdout and ends the way Desktop Commander ended (its exit status, or the same signal; a signal that would dump
  core becomes exit 128+N instead), and Desktop Commander never outlives it. Exit 5 with a fix message if `computer mcp install` has
  not been run. Desktop Commander runs with `--no-onboarding`: until its welcome page has been opened once in a HOME it may
  otherwise answer `initialize` only after opening that page with `xdg-open`, which never returns while a newly started browser
  stays open.
- No client is offered Desktop Commander's feedback channel: `give_feedback_to_desktop_commander` is left out of the tool list
  and a call to it is answered with an error without starting anything (Desktop Commander's own handler opens the feedback form
  with `xdg-open` on this computer and waits for it), and the survey request Desktop Commander adds to a tool result when its
  remote `user_surveys` flag is on is removed before the client sees it. Desktop Commander 0.3.2 has no setting that hides the
  tool, and its survey opt-out (`feedbackGiven` in its config) is one the model can set back, so `computer mcp serve` relays
  Desktop Commander's stdio and leaves out exactly these; everything else passes through unchanged.
  To give the Desktop Commander team feedback, open https://tally.so/r/mKqoKg yourself.

### PDF output on Ubuntu 23.10+ (AppArmor)

Desktop Commander's `write_pdf` renders with Chrome. It first uses the Chrome for Testing build in its own cache
(`~/.claude-server-commander/puppeteer-cache/chrome/<build>/chrome-linux64/chrome`), then a packaged browser at
`/usr/bin/google-chrome`, `/usr/bin/google-chrome-stable`, `/usr/bin/chromium`, `/usr/bin/chromium-browser` or
`/snap/bin/chromium`, and with neither it downloads Chrome for Testing into that cache. Ubuntu 23.10+ sets
`kernel.apparmor_restrict_unprivileged_userns=1`, and a program no AppArmor profile allows `userns,` for cannot start Chrome's
sandbox: write_pdf then fails with `No usable sandbox!`. Packaged browsers bring their own profile (or a setuid sandbox, or snap
confinement); the downloaded build has none. The shell and file tools are not affected.

`computer mcp install` (and so `computer claude install` and `computer chatgpt install`) prints a `warning:` when the restriction
is on and no profile in `/etc/apparmor.d` covers the Chrome write_pdf will launch; `computer chatgpt doctor` shows it as a `[warn]`
check and `computer claude status` as a `[warn] pdf:` line. When several profiles match that Chrome, the most specific one
decides, as in the kernel: the one that matches more of the path before its first `*` (an exact path matches all of it), and a
tie applies none. A profile on disk counts only once the kernel lists a profile of that name as loaded, in
`/sys/kernel/security/apparmor/profiles` (readable by root) or `/sys/kernel/security/apparmor/policy/profiles` (readable by any
user); when neither can be read or the kernel does not list it, the warning says the coverage is unverified and names the
`apparmor_parser -r` command that loads it. A profile edited after it was loaded is judged by its file, so reload it after an
edit. Neither changes their exit code, and nothing here changes system settings: both fixes need root, following
[Chromium's guide](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md).

1. Recommended: an AppArmor profile for the downloaded build only. Save this as `/etc/apparmor.d/desktop-commander-chrome` and
   load it with `sudo apparmor_parser -r /etc/apparmor.d/desktop-commander-chrome` (the `*` keeps it valid when Desktop Commander
   updates its build). Any program at that path, which your user can write, may create user namespaces; that is still much
   narrower than option 2.

   ```
   abi <abi/4.0>,
   include <tunables/global>

   profile desktop-commander-chrome @{HOME}/.claude-server-commander/puppeteer-cache/chrome/*/chrome-linux64/chrome flags=(unconfined) {
     userns,

     # Site-specific additions and overrides. See local/README for details.
     include if exists <local/desktop-commander-chrome>
   }
   ```

2. Lift the restriction for every program: `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (add
   `kernel.apparmor_restrict_unprivileged_userns=0` to a file in `/etc/sysctl.d/` to keep it after a reboot). This switches off a
   system-wide hardening, so prefer option 1.

Installing a packaged Google Chrome or Chromium does not help while the cache holds a build: Desktop Commander prefers the cache.

`computer mcp install` downloads that build itself (puppeteer's postinstall, run with node directly). The download gets npm's
`node-options` setting as `NODE_OPTIONS`, as it would under npm: when an npmrc sets it (for example `node-options=--use-system-ca`
behind a TLS-intercepting proxy), it replaces your own `NODE_OPTIONS` for the download instead of being appended, because two
option sets can conflict (`--use-openssl-ca` with `--use-bundled-ca`) and node then does not start. With no npm setting, your
`NODE_OPTIONS` is kept.

## Claude Desktop: `computer claude`

- `computer claude install` sets `mcpServers["desktop-commander"]` in `${XDG_CONFIG_HOME:-~/.config}/Claude/claude_desktop_config.json`
  to the absolute path of this `computer` executable with `mcp serve`, runs `computer mcp install`, and makes sure
  `~/.config/autostart/claude-desktop.desktop` exists, is enabled and starts a program that exists. Every other key of the config
  is preserved, the write is atomic, a timestamped backup is made first, and invalid JSON is refused (exit 2) without touching the
  file. An autostart entry that is fine is never rewritten (it may carry your own Exec; the entry is read and judged right
  before a repair, so one fixed in the meantime is kept); a disabled one is re-enabled, one whose Exec is missing, cannot
  be parsed (an unterminated quote) or names a program that does not exist or is not executable gets only its Exec line
  replaced, and an empty file or one without a `[Desktop Entry]` section is replaced; each after a backup. The Exec written names
  `/usr/lib/claude-desktop/claude-desktop` when it is executable, else `claude-desktop` found in an absolute PATH entry,
  quoted and escaped per the Desktop Entry spec. Claude Desktop is never restarted by this tool: restart it yourself to pick the
  change up.
- `computer claude status` checks the config entry, the executable, the autostart entry (present, has a `[Desktop Entry]`
  section, enabled, Exec parses the way GIO parses it when GNOME starts it at login and names an executable program), and
  performs a real MCP handshake
  (`initialize`, `tools/list`) against the configured command under a minimal environment. Exit 0 healthy, 3 unhealthy.
- `computer claude uninstall` removes only the `desktop-commander` entry (after a backup); the managed install and the autostart
  entry stay.

Design: `docs/superpowers/specs/2026-11-11-claude-desktop-support-design.md`.

## ChatGPT: `computer chatgpt`

### One command

```sh
computer chatgpt install --domain example.com
```

On a machine whose hostname is `ubuntu-pc` this serves ChatGPT at `https://ubuntu-pc-mcp.example.com/mcp` through a Cloudflare
tunnel named `ubuntu-pc`. `--hostname dc.example.com` sets the full name instead (pass one of the two; it replaces a saved
domain); both are saved, so a re-run needs no flags.

`computer chatgpt install` does everything, idempotently (re-running changes only what is missing or different):

1. **Checks prerequisites before writing anything**: pinned builds for this CPU (x86_64 or aarch64, exit 2 otherwise), `node`, `npm`,
   `loginctl` (exit 127 with the install command), a reachable systemd user manager, the `computer` executable the units will run,
   that the hostname's zone is served by Cloudflare (DNS-over-HTTPS NS lookup; exit 2 when it is not, or when `--domain` is not the
   zone itself; a warning when the lookup itself fails), and that the loopback ports are free or already this stack's (exit 2 with
   the flag to pass otherwise).
2. **Installs**: the shared Desktop Commander (`computer mcp install`), `mcp-auth-proxy` v2.10.2 and `cloudflared` 2026.10.7 (pinned
   release assets, sha256-verified before they are placed) and a generated login password (`~/.config/computer/chatgpt/auth.env`,
   mode 600, never printed, logged or put in an argument list).
3. **Cloudflare**: when this target has no account certificate yet, runs `cloudflared tunnel login` (the ONLY interactive step: a
   URL where you authorize the zone of your hostname, see [A new machine](#a-new-machine-login-or-an-existing-certificate)), creates
   the named tunnel (default: the machine name, `--tunnel-name` to choose), and routes the hostname to it. A tunnel of that name
   that already exists with credentials in this target's directory (`<tunnel name>.json`, or `<tunnel id>.json` as plain
   `cloudflared tunnel create` names it) is reused, but only when the file's TunnelID is the id Cloudflare lists for that name: a
   file of another id (the tunnel was deleted and created again, most likely by a machine of the same name) is refused with exit 2.
   An old credentials file without a TunnelID (only AccountTag and TunnelSecret, as older cloudflared releases wrote it) is
   bound to a tunnel only by its file name: `<tunnel id>.json` here is taken and copied to `<tunnel name>.json` with the TunnelID
   added. An old `<tunnel name>.json` (for example copied from such a file by hand) is refused with exit 2, even with
   `--force-takeover`, since nothing in it says which tunnel it is: copy the original here under the name cloudflared gave it
   when it created the tunnel (`<tunnel id>.json`, for example from `~/.cloudflared`) and re-run, and the TunnelID is added to
   `<tunnel name>.json`. Never name a copy after the id Cloudflare lists now: only the name cloudflared gave the file shows which
   tunnel it is, and a stale file bound to a tunnel recreated by a same-name machine would point this machine at that tunnel.
   One that exists WITHOUT credentials here (another machine of the same name, or a lost install) is refused with exit 2: pass
   `--machine-name`, delete the stale tunnel, or adopt it by naming it with `--tunnel-name`. The hostname is routed by the id of
   the tunnel checked above, never by its name: cloudflared resolves a name through a `credentials-file` named in its config
   first, which may be another tunnel's (so the command `--skip-dns` prints to run by hand names this target's binary, config
   and certificate, reads the id from the tunnel's `<name>.json` when it runs and stops when that holds no tunnel id, and drops
   an exported `TUNNEL_FORCE_PROVISIONING_DNS`). A NEW DNS record is created without a question; an EXISTING record that points
   elsewhere is replaced only after you confirm (`--yes` answers yes; without consent install stops with exit 2).
   cloudflared's certificate, tunnel credentials and `config.yml` live in this target's own directory
   (`~/.config/computer/chatgpt/cloudflared`, mode 700; `--cloudflared-dir` to choose). `~/.cloudflared` is never read or written unless
   you point `--cloudflared-dir` at it, so a tunnel that already runs on this machine is left alone.

   **Same-name machines.** Two machines with the same name must never serve one hostname: Cloudflare would split ChatGPT's
   traffic between them and the other machine's connector would fail with 401s. So adopting a tunnel with `--tunnel-name` is
   refused while Cloudflare reports live connections for it (`cloudflared tunnel info`: a cloudflared somewhere runs it; recently
   closed connections do not count, and an answer without connection data, for the tunnel or for any one of its connectors, is
   refused too), and the existing record of a `--domain` hostname (`<machine>-mcp.<zone>`, which a same-name machine derives
   too) is never replaced by `--yes`: install stops with exit 2 and names the record. `--force-takeover` passes both refusals; it
   is DANGEROUS, use it only when you are sure no other machine serves the tunnel or the hostname (for example the record of this
   machine's own 0.2.x install). Your own running tunnel, with its credentials here, needs no override.
4. **Starts**: writes the systemd user units (only now, so a run that stopped in step 3 leaves them as they were and the corrected
   re-run still restarts what changed), enables lingering (the stack starts at boot without a login), enables and starts the units
   (restarting only those whose file changed), and waits until the local chain answers.
5. **Waits for the public URL** (`--timeout`, default 180 s) until `https://<hostname>/mcp` answers 401 with the
   `WWW-Authenticate: Bearer resource_metadata=...` challenge ChatGPT's OAuth discovery needs.
6. **Prints the ChatGPT connector fields**: where to add it (Plugins -> "+" -> *Add custom MCP server*), Name (the machine name;
   `--name` to choose), Connection *Server URL*,
   Server URL `https://<hostname>/mcp`, Authentication *OAuth* (dynamic client registration, nothing to type), the risk checkbox,
   *Create as a plugin*, and the file that holds the login password ChatGPT's login page asks for. If the URL did not answer in time
   it still prints them and exits 7.

ChatGPT cannot be pre-filled (no link, API or CLI exists), so the clicking is yours. `computer chatgpt connect` then watches the
connection live and says "connected" only after a real `tools/call` from a client the shim could verify as ChatGPT.

### Several machines

Run the same command on every machine; each gets its own tunnel, public URL and ChatGPT connector:

```sh
computer chatgpt install --domain example.com   # on ubuntu-pc: https://ubuntu-pc-mcp.example.com/mcp, tunnel ubuntu-pc
computer chatgpt install --domain example.com   # on odroid:    https://odroid-mcp.example.com/mcp,    tunnel odroid
```

Then add one connector per machine in ChatGPT; `connect` and `install` suggest the machine name as the connector name, so they stay
apart. The machine name is the short system hostname turned into a DNS label (lowercase; anything but letters and digits becomes
`-`; at most 59 characters, so `<machine>-mcp` fits one 63-character label); `--machine-name <name>` chooses it. It and the domain are
saved in `~/.config/computer/chatgpt/config.toml`, so a later hostname change does not move the URL.

Why `<machine>-mcp.<domain>` and not `<machine>.mcp.<domain>`: Cloudflare's free Universal SSL certificate covers the zone apex and
first-level subdomains only; deeper names "are not covered and will not serve a valid certificate" without a paid Advanced
Certificate ([Cloudflare: Universal SSL limitations](https://developers.cloudflare.com/ssl/edge-certificates/universal-ssl/limitations/),
checked 2026-11-18). So `--domain` must be the zone itself; install refuses a subdomain of it.

Two machines with the same hostname would collide on the tunnel name (tunnel names are per Cloudflare account): the second one is
refused (see step 3) and `--machine-name` gives it another name. A configuration made with `--hostname` keeps working unchanged.

CPUs: x86_64 and aarch64 (`mcp-auth-proxy` publishes Linux builds for amd64 and arm64 only), so an ARM board such as an Odroid needs
a 64-bit OS; on another CPU install stops before writing anything. macOS is out of scope (the stack runs as systemd user units).

### A new machine: login or an existing certificate

The first `install` on a machine without a Cloudflare account certificate runs `cloudflared tunnel login`. It prints a URL (shown
again on its own line) and waits; on a desktop it is opened in the browser, on a headless machine (no display, e.g. over ssh) open
it on any device, log in to Cloudflare and authorize the zone; the command then continues by itself.

Without a browser at all, reuse the certificate of a machine that is already logged in:

```sh
computer chatgpt install --domain example.com \
  --cloudflare-cert <(ssh ubuntu-pc cat .config/computer/chatgpt/cloudflared/cert.pem)
```

The process substitution (bash, zsh) streams the certificate straight from the other machine: no copy is left on disk, whatever
the exit code.

The certificate is copied mode 600 into this target's cloudflared directory (a different certificate already there is refused, not
replaced). It lets its holder create tunnels and DNS records in the account, so treat it like a password. A file the pinned
cloudflared would not read is refused before anything is written: base64 broken by anything but spaces, tabs and line breaks, a
token that is not UTF-8 JSON, or one that sets a field twice in any letter case (`apiToken` and `APITOKEN`: cloudflared matches
names ignoring case and would read the last).

### How it runs

- **mcp-auth-proxy** does all authentication (OAuth 2.1, dynamic client registration, PKCE, a password login page); its stdio child
  is the absolute `computer mcp serve` command, so Desktop Commander comes from the shared core.
- **shim** adds what ChatGPT's setup needs and the proxy lacks: a `WWW-Authenticate: Bearer resource_metadata=...` challenge on every
  401 outside the login pages, the path-suffix form of `/.well-known/oauth-protected-resource`, and an SSE comment on an idle event
  stream so Cloudflare releases the headers. It also writes the access log. It passes request bodies on as they arrive (never
  holding one whole before the proxy has checked the login) and serves at most 128 connections at once, answering more with 503.
- **cloudflared** runs the named tunnel with `--config`, `--origincert` and `--credentials-file` from this target's directory.
- Units `computer-chatgpt-{bridge,shim,tunnel}.service` (`--unit-prefix` to rename) run with `Restart=always` and
  `StartLimitIntervalSec=0`: a crash loop or a network that is down at boot never leaves a unit dead for good.

### Commands

| Command | What it does |
|---|---|
| `install` | the one-command setup above. Flags: `--domain`, `--machine-name`, `--hostname`, `--tunnel-name`, `--cloudflare-cert`, `--cloudflared-dir`, `--proxy-port`, `--shim-port`, `--metrics-port`, `--unit-prefix`, `--yes`, `--force-takeover`, `--skip-dns`, `--no-linger`, `--timeout`, `--name` (default: the machine name) |
| `init` | building block: choose the mode (default `public`), write the config, install the pinned pieces, create the password (public) and unit files. Flags: `--mode`, `--domain`, `--machine-name`, `--hostname`, `--scheme`, `--tunnel-name`, `--tunnel-id`, `--tunnel-health-port`, `--proxy-port`, `--shim-port`, `--metrics-port`, `--unit-prefix`, `--cloudflared-dir`, `--lang`, `--no-install`, `--no-units` |
| `login` | building block: public: Cloudflare login, create the named tunnel (an existing one without local credentials only when named with `--tunnel-name`, and while it has live connections only with `--force-takeover`), route the hostname (asks before replacing a record, keeping it exits 2; the record of a `--domain` hostname only with `--force-takeover`). Flags: `--tunnel-name`, `--cloudflare-cert`, `--yes`, `--force-takeover`, `--skip-dns`. Tunnel mode: store the runtime API key. Flag: `--key-stdin` |
| `start` | enable and start the stack, wait until it serves. Flags: `--no-wait`, `--no-linger` |
| `stop` | stop the stack now (stays enabled for the next boot) |
| `restart` | restart in start order and wait until it serves |
| `end` | stop and disable; `--revoke` also deletes all OAuth sessions and registered clients (tunnel mode: the stored runtime key). Fails (exit 1) when a unit is still running or enabled afterwards, and refuses a symlinked data dir |
| `status` | units, local probes, last request and verified ChatGPT tool calls (tunnel mode: the daemon's state and counters); `--json`; exit 3 when unhealthy |
| `logs` | the compact access log. Flags: `-n`/`--lines`, `--all`, `--tool`, `--rpc`, `--status`, `--client`, `--oauth` |
| `doctor` | diagnose the chain end to end and say how to fix it, never restarts (tunnel mode: its own chain, nothing public). Flags: `--local`, `--json`; `--heal` instead runs one watchdog pass that restarts a hung unit (below) |
| `connect` | ChatGPT dialog fields, clipboard, live checklist. Flags: `--name` (default: the machine name), `--no-open`, `--no-copy`, `--no-wait`, `--timeout` |
| `run` | run the stack in the foreground without systemd (development, proofs on scratch ports); `--no-tunnel` (public mode) |
| `uninstall` | remove the unit files; `--purge` also deletes config, password, tunnel key, OAuth state, logs, binaries and the default cloudflared directory. Without a config, `--purge` also stops and removes the unit files this tool wrote under another `--unit-prefix` (found by their marker line). The shared Desktop Commander, the Cloudflare tunnel and its DNS record are kept (it says so) |
| `shim`, `quiet` | internal: what the units run. `shim` takes `--external-url`, `--listen-port`, `--upstream-port`, `--log-file`, `--oauth-db` (the proxy's OAuth store, read only) |

As full command lines: `computer chatgpt install`, `computer chatgpt init`, `computer chatgpt login`, `computer chatgpt start`,
`computer chatgpt stop`, `computer chatgpt restart`, `computer chatgpt end`, `computer chatgpt status`, `computer chatgpt logs`,
`computer chatgpt doctor`, `computer chatgpt connect`, `computer chatgpt run`, `computer chatgpt uninstall`, `computer chatgpt shim`,
`computer chatgpt quiet`.

### Healing a hung unit (`doctor --heal`)

`Restart=always` restarts a unit whose process exits. A process that stays up but stops answering is not restarted: a stopped
(SIGSTOP) or deadlocked proxy keeps its port, connections are accepted and requests hang, and the shim in front of it hangs the
same way; cloudflared with no edge connection stays active and answers its `/ready` with 503. `computer chatgpt doctor --heal`
(public mode) is one bounded watchdog pass for that case:

- it probes only locally: the bridge on the proxy port, the shim on the shim port (a shim that hangs while the bridge does not serve
  is counted against the bridge, not restarted), and, with a tunnel, cloudflared's own `127.0.0.1:<metrics port>/ready`. The public
  URL is never probed by `--heal`: its failures (DNS, Cloudflare, the internet) are not fixed by a restart, so plain `doctor`
  reports them and nothing acts on them;
- only an `active` unit is restarted (a failed or restarting one is systemd's job), after 3 failing passes in a row, at most 3 times
  an hour per unit and 10 minutes apart; once the budget is spent it says so and exits 3 instead of looping;
- a stop wins: a unit stopped while heal runs stays stopped, and the shim or the tunnel is not restarted while a unit it starts
  with (`Wants=`: the bridge, and for the tunnel the shim too) is stopped when heal decides, since the restart would start it;
- no restart while the internet is unreachable (no TCP answer from 1.1.1.1:443 or 8.8.8.8:443, checked only when something
  failed): one line says so and the failure counts start again after the outage;
- a lock (`~/.local/state/computer/chatgpt/heal.lock`) keeps two passes from overlapping; the counts and restart times are in
  `heal.json` next to it;
- under systemd, a restart, a restart that failed and the first pass a spent budget stops one are logged at warning priority, a
  counted failure, a held-back restart and the offline gate at info, everything else (a healthy pass, a unit that is not active)
  at debug.

Nothing schedules it for you. To run it every two minutes, add a user timer (`command -v computer` gives the path for `ExecStart`):

```ini
# ~/.config/systemd/user/computer-chatgpt-heal.service
[Unit]
Description=Restart a hung unit of the computer chatgpt stack

[Service]
Type=oneshot
ExecStart=%h/.local/bin/computer chatgpt doctor --heal

# ~/.config/systemd/user/computer-chatgpt-heal.timer
[Timer]
OnBootSec=5min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
```

then `systemctl --user daemon-reload && systemctl --user enable --now computer-chatgpt-heal.timer`, and
`journalctl --user -u computer-chatgpt-heal.service -p warning` lists what it did.

### Modes

`install` always sets up `public`. `init` shows this comparison and changes nothing you did not ask for:

```
                              public                            tunnel
needs a domain + Cloudflare   yes                               no
needs an OpenAI Platform key  no                                yes (Tunnels Read+Use)
URL on the internet           yes (OAuth login)                 no
processes                     4 (proxy, shim, cloudflared, DC)  2 (tunnel-client, DC)
ChatGPT app must be open      no                                no
works while the PC sleeps     no                                no
request time limit            ~100 s (Cloudflare)               120 s (OpenAI)
proven on this PC             yes                               not yet
```

The default is `public` and never changes by itself: no command, no fallback and no detected prerequisite switches to `tunnel`; only
`init --mode tunnel` does, explicitly. `init` refuses to switch while the other mode's units are installed (`uninstall` first).

### Tunnel mode (experimental)

OpenAI's `tunnel-client` v0.0.17 runs `computer mcp serve` over stdio and relays ChatGPT's requests through OpenAI's own tunnel service
([Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)): no domain, no Cloudflare, no proxy or shim,
nothing listening on the internet. One unit, `computer-chatgpt-tunnel-client.service`.

```sh
computer chatgpt init --mode tunnel --tunnel-id tunnel_...   # pinned tunnel-client + the shared Desktop Commander, one unit
computer chatgpt login --key-stdin < runtime-key              # store the runtime API key (or run login for a hidden prompt)
computer chatgpt start                                        # waits until tunnel-client is ready and polling OpenAI
computer chatgpt connect                                      # Tunnel-connection fields for ChatGPT, then a live checklist
```

- You need an OpenAI Platform tunnel, associated with your ChatGPT workspace, and a runtime API key restricted to **Tunnels Read+Use**.
  Create both in the Platform; this tool never creates or manages them.
- The key is stored in `~/.config/computer/chatgpt/tunnel.key` (mode 600, even over an older wider file) and reaches the daemon as
  `file:<path>`: never in an argument list, the environment, a unit, a log or any output. `end --revoke` deletes the local copy;
  `uninstall --purge` deletes it with the rest.
- tunnel-client takes its MCP command as one string split on whitespace, so the path of `computer` must not contain spaces or commas
  (`init` refuses it otherwise). Exactly one daemon may serve a tunnel id. The health endpoint is `127.0.0.1:8794`
  (`--tunnel-health-port`).
- `status`, `doctor` and the `connect` checklist read the daemon's own health port: `unavailable`, `not_ready`, `awaiting_poll`,
  `awaiting_discovery`, `awaiting_tool_call`, `connected`. `connected` means a `tools/call` came through **your tunnel**; the daemon
  cannot say which OpenAI surface sent it.

**Proven** (opt-in test): the real `tunnel-client` v0.0.17 binary, started with the generated command line against a stand-in control
plane and a stub stdio MCP server, polls with the key from its file, forwards `initialize`, `tools/list` and `tools/call`, and the state
machine above follows it. **Not proven**: a real ChatGPT tool call through OpenAI's hosted tunnel.

### The access log

`~/.local/state/computer/chatgpt/access.log`, one line per request, rotated by size, mode 600:

```
<ISO time> <status> <method> <path> <latency>ms client=<label> [rpc=<method>] [tool=<name>] [suppressed=<n>]
```

Never written: query strings, bodies, tool arguments, Authorization headers, cookies, passwords, tokens or the full User-Agent.
`client=` is the User-Agent kind (for example `openai-mcp`) only when a second signal agrees: the call carries a token the proxy
accepted and that token belongs to an OAuth client registered with `https://chatgpt.com/...` redirect URIs. Anything else is
`client=unverified`. The shim learns which client a token belongs to from the token exchange it forwards; for a token it never saw
issued (one ChatGPT already held before a restart, a reinstall or a lost state directory) it reads the client from mcp-auth-proxy's
own OAuth store, read only, so a valid ChatGPT token keeps its label; that lookup reads a few KiB per call, so even a multi-MiB
registration a stranger stores next to the token cannot hide it. A request with two Authorization headers is answered 400 and never
forwarded: the proxy would check one and the label could name another.

### Security

Desktop Commander is a shell on this PC. The public URL is guarded by the OAuth login with the generated password; keep the password
file private, and use `computer chatgpt end --revoke` to drop every issued token. Cloudflare's per-request limit is ~100 s.

Design: `docs/superpowers/specs/2026-11-17-chatgpt-target-design.md`. Moving an existing hand-made stack under this tool: `CUTOVER.md`.

## Exit codes

0 ok, 1 internal error or a failed gate, 2 usage or configuration, 3 unhealthy or not connected, 5 something missing, 7 network,
8 permission or credential, 127 a required tool is not installed. Failures print what, why and how to fix.

## Development

```sh
uv sync --group dev
uv run --group dev ruff check . && uv run --group dev pyright && uv run --group dev pytest -q
COMPUTER_INTEGRATION=1 COMPUTER_PROXY_BIN=/path/to/mcp-auth-proxy uv run --group dev pytest tests/chatgpt/test_end_to_end.py
COMPUTER_INTEGRATION=1 COMPUTER_TUNNEL_BIN=/path/to/tunnel-client uv run --group dev pytest tests/chatgpt/test_tunnel_end_to_end.py
COMPUTER_BENCHMARK=1 uv run --group dev pytest tests/chatgpt/test_shim_uploads.py -k benchmark
```

Tests are hermetic (scratch HOME, no network, fake systemctl/cloudflared/npm). The integration tests start the real `mcp-auth-proxy`
(with `computer mcp serve` as its child) or the real `tunnel-client` against a stand-in control plane, on scratch ports. Never develop
against a running install: use a scratch `HOME` and other ports (see AGENTS.md). Timing benchmarks (the shim's keep-alive latency)
run only with `COMPUTER_BENCHMARK=1`: a loaded machine would fail them with nothing broken.
