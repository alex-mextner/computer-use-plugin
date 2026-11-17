# computer

Give a chat client (Claude Desktop or ChatGPT) a shell and files on this PC through MCP (Desktop Commander).

Two targets, each set up by ONE command:

| Target | One command | Needs on this PC / account |
|---|---|---|
| **Claude Desktop** | `computer claude install` | Linux; the Claude Desktop app; Node.js 18+ and npm |
| **ChatGPT** | `computer chatgpt install --hostname computer.example.com` | Linux with systemd `--user` (and `loginctl`); Node.js 18+ and npm; a Cloudflare account with your domain (zone) on it; a ChatGPT plan that lets you add custom MCP servers with write tools (see below). `cloudflared` and `mcp-auth-proxy` are downloaded automatically, pinned and sha256-verified |

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
- `computer mcp serve` is what a client launches: it replaces itself with the pinned Desktop Commander over stdio, needs
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

## ChatGPT: `computer chatgpt`

### One command

```sh
computer chatgpt install --hostname computer.example.com
```

`computer chatgpt install` does everything, idempotently (re-running changes only what is missing or different):

1. **Checks prerequisites before writing anything**: `node`, `npm`, `loginctl` (exit 127 with the install command), a reachable systemd
   user manager, the `computer` executable the units will run, that the hostname's zone is served by Cloudflare (DNS-over-HTTPS NS
   lookup; exit 2 when it is not, a warning when the lookup itself fails), and that the loopback ports are free or already this
   stack's (exit 2 with the flag to pass otherwise).
2. **Installs**: the shared Desktop Commander (`computer mcp install`), `mcp-auth-proxy` v2.10.2 and `cloudflared` 2026.10.7 (pinned
   release assets, sha256-verified before they are placed), a generated login password (`~/.config/computer/chatgpt/auth.env`, mode 600,
   never printed, logged or put in an argument list) and the systemd user units.
3. **Cloudflare**: when this target has no account certificate yet, runs `cloudflared tunnel login` (the ONLY interactive step: a
   browser page where you authorize the zone of your hostname), creates the named tunnel (default `computer-<hostname with dashes>`,
   `--tunnel-name` to choose), and routes the hostname to it. A NEW DNS record is created without a question; an EXISTING record that
   points elsewhere is replaced only after you confirm (`--yes` answers yes; without consent install stops with exit 2).
   cloudflared's certificate, tunnel credentials and `config.yml` live in this target's own directory
   (`~/.config/computer/chatgpt/cloudflared`, mode 700; `--cloudflared-dir` to choose). `~/.cloudflared` is never read or written unless
   you point `--cloudflared-dir` at it, so a tunnel that already runs on this machine is left alone.
4. **Starts**: enables lingering (the stack starts at boot without a login), enables and starts the units (restarting only those whose
   file changed), and waits until the local chain answers.
5. **Waits for the public URL** (`--timeout`, default 180 s) until `https://<hostname>/mcp` answers 401 with the
   `WWW-Authenticate: Bearer resource_metadata=...` challenge ChatGPT's OAuth discovery needs.
6. **Prints the ChatGPT connector fields**: where to add it (Plugins -> "+" -> *Add custom MCP server*), Name, Connection *Server URL*,
   Server URL `https://<hostname>/mcp`, Authentication *OAuth* (dynamic client registration, nothing to type), the risk checkbox,
   *Create as a plugin*, and the file that holds the login password ChatGPT's login page asks for. If the URL did not answer in time
   it still prints them and exits 7.

ChatGPT cannot be pre-filled (no link, API or CLI exists), so the clicking is yours. `computer chatgpt connect` then watches the
connection live and says "connected" only after a real `tools/call` from a client the shim could verify as ChatGPT.

### How it runs

- **mcp-auth-proxy** does all authentication (OAuth 2.1, dynamic client registration, PKCE, a password login page); its stdio child
  is the absolute `computer mcp serve` command, so Desktop Commander comes from the shared core.
- **shim** adds what ChatGPT's setup needs and the proxy lacks: a `WWW-Authenticate: Bearer resource_metadata=...` challenge on every
  401 outside the login pages, the path-suffix form of `/.well-known/oauth-protected-resource`, and an SSE comment on an idle event
  stream so Cloudflare releases the headers. It also writes the access log.
- **cloudflared** runs the named tunnel with `--config`, `--origincert` and `--credentials-file` from this target's directory.
- Units `computer-chatgpt-{bridge,shim,tunnel}.service` (`--unit-prefix` to rename) run with `Restart=always` and
  `StartLimitIntervalSec=0`: a crash loop or a network that is down at boot never leaves a unit dead for good.

### Commands

| Command | What it does |
|---|---|
| `install` | the one-command setup above. Flags: `--hostname`, `--tunnel-name`, `--cloudflared-dir`, `--proxy-port`, `--shim-port`, `--metrics-port`, `--unit-prefix`, `--yes`, `--skip-dns`, `--no-linger`, `--timeout`, `--name` |
| `init` | building block: choose the mode (default `public`), write the config, install the pinned pieces, create the password (public) and unit files. Flags: `--mode`, `--hostname`, `--scheme`, `--tunnel-name`, `--tunnel-id`, `--tunnel-health-port`, `--proxy-port`, `--shim-port`, `--metrics-port`, `--unit-prefix`, `--cloudflared-dir`, `--lang`, `--no-install`, `--no-units` |
| `login` | building block: public: Cloudflare login, create the named tunnel, route the hostname (asks before replacing a record). Flags: `--tunnel-name`, `--yes`, `--skip-dns`. Tunnel mode: store the runtime API key. Flag: `--key-stdin` |
| `start` | enable and start the stack, wait until it serves. Flags: `--no-wait`, `--no-linger` |
| `stop` | stop the stack now (stays enabled for the next boot) |
| `restart` | restart in start order and wait until it serves |
| `end` | stop and disable; `--revoke` also deletes all OAuth sessions and registered clients (tunnel mode: the stored runtime key). Fails (exit 1) when a unit is still running or enabled afterwards, and refuses a symlinked data dir |
| `status` | units, local probes, last request and verified ChatGPT tool calls (tunnel mode: the daemon's state and counters); `--json`; exit 3 when unhealthy |
| `logs` | the compact access log. Flags: `-n`/`--lines`, `--all`, `--tool`, `--rpc`, `--status`, `--client`, `--oauth` |
| `doctor` | diagnose the chain end to end and say how to fix it, never restarts (tunnel mode: its own chain, nothing public). Flags: `--local`, `--json` |
| `connect` | ChatGPT dialog fields, clipboard, live checklist. Flags: `--name`, `--no-open`, `--no-copy`, `--no-wait`, `--timeout` |
| `run` | run the stack in the foreground without systemd (development, proofs on scratch ports); `--no-tunnel` (public mode) |
| `uninstall` | remove the unit files; `--purge` also deletes config, password, tunnel key, OAuth state, logs, binaries and the default cloudflared directory. The shared Desktop Commander, the Cloudflare tunnel and its DNS record are kept (it says so) |
| `shim`, `quiet` | internal: what the units run. `shim` takes `--external-url`, `--listen-port`, `--upstream-port`, `--log-file` |

As full command lines: `computer chatgpt install`, `computer chatgpt init`, `computer chatgpt login`, `computer chatgpt start`,
`computer chatgpt stop`, `computer chatgpt restart`, `computer chatgpt end`, `computer chatgpt status`, `computer chatgpt logs`,
`computer chatgpt doctor`, `computer chatgpt connect`, `computer chatgpt run`, `computer chatgpt uninstall`, `computer chatgpt shim`,
`computer chatgpt quiet`.

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
`client=unverified`.

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
```

Tests are hermetic (scratch HOME, no network, fake systemctl/cloudflared/npm). The integration tests start the real `mcp-auth-proxy`
(with `computer mcp serve` as its child) or the real `tunnel-client` against a stand-in control plane, on scratch ports. Never develop
against a running install: use a scratch `HOME` and other ports (see AGENTS.md).
