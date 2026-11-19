# Changelog

## Unreleased

- The ChatGPT bridge no longer writes a journal line per request with the OAuth login's query strings (client_id,
  redirect_uri, state): mcp-auth-proxy logs its access records to stderr, which `quiet` passed through untouched. `quiet` now
  filters stderr like stdout, each stream on its own so a silent one never holds back the other; errors and warnings still reach
  the journal on the stream they came from. `quiet` ends when its command does, even if a process it started still holds a
  stream open, and when it cannot write a line it stops the command and fails instead of hanging (#26).
- `computer chatgpt run` stopped by one of its processes dying of a signal now ends the same way (killed by SIGTERM, 143 in a
  shell) instead of exiting 241, so a clean stop no longer looks like a crash. It stops the other processes first, and shares the
  rule with `quiet` (#28).

## 0.4.5

- A chat client can no longer open a browser on your computer through Desktop Commander's feedback tool, or hang a tool call
  until that browser is closed: `computer mcp serve` (used by Claude and ChatGPT alike) leaves
  `give_feedback_to_desktop_commander` out of the tool list and answers a call to it with an error without running `xdg-open`,
  and removes the survey request Desktop Commander adds to a tool result when its remote `user_surveys` flag is on, so the model
  is never asked to run that survey. Desktop Commander 0.3.2 has no setting that hides the tool, so serve now relays its stdio
  instead of replacing itself with it; everything else passes through unchanged, serve still ends the way Desktop Commander
  ended, and Desktop Commander stops when serve is killed. A request nested more than 256 levels deep is answered with an
  invalid-request error instead of being passed on, the same on every Python version (#24).

## 0.4.4

- `computer chatgpt doctor --heal` keeps the limits it promises when things are slow or go wrong: a local probe now gives up
  after its timeout even when the server trickles bytes or never ends a chunked answer, so a pass cannot hang on the lock;
  spacing and the hourly budget count from when a restart really ran, not from the start of a slow pass; a restart that errors
  no longer drops the other units' results of that pass; the tunnel or shim is not restarted while a unit it starts with
  (`Wants=`) is stopped when heal decides, so it does not bring back a bridge or shim the owner stopped; and a refused
  `try-restart` of a unit that still runs is reported as a failed restart instead of "stopped during the pass" (#61).
- `computer chatgpt uninstall --purge` without a config no longer leaves dangling enablement links behind: a unit whose file was
  already deleted kept its link in `default.target.wants/` (or another `.wants/`/`.requires/` directory), which `disable` cannot
  drop without the file; a link that still reaches a unit file elsewhere is kept. It also no longer crashes on a
  `*-<service>.service` file it cannot read (mode 000, or deleted between the listing and the read): such a
  file is skipped as not its own, and the managed units are still stopped (#52).

## 0.4.2

- A stranger can no longer make a valid ChatGPT token read `client=unverified` after a shim restart: the shim refused any page
  of mcp-auth-proxy's OAuth store bigger than 1 MiB, so one open registration with a multi-MiB redirect URI list that landed next
  to ChatGPT's token hid it. The lookup now reads only the elements it searches and the one record it needs (at most 256 KiB),
  a few KiB per call whatever else the store holds. A request with more than one Authorization header is now answered 400 and
  not forwarded, since the proxy checks one of them and the label could name the client of another (#60).
- The ChatGPT shim relays mcp-auth-proxy's 401 at once to a chunked upload that pauses inside a chunk-size line, a chunk end or
  a trailer, and frees its connection slot. It read such a line to its end once one byte of it had come, so the 401 waited for
  the 65 s idle timeout and a stranger's paused uploads could hold slots until the owner's connector got 503 (#56).
- The shim's Nagle test now checks that `TCP_NODELAY` is on for every connection the shim accepts instead of timing 200
  requests, which a busy CI runner could fail with the fix working; the timing is a benchmark run with `COMPUTER_BENCHMARK=1`
  (#66).
## 0.4.1

- The ChatGPT shim no longer delays each small response by about 40 ms: it wrote the headers and the body in two writes with
  Nagle's algorithm on, so the body waited for the client's delayed ACK. It now sends them at once; 200 keep-alive `POST /mcp`
  on loopback take well under 10 ms each instead of 41 ms (#51).

## 0.4.0

- `computer chatgpt doctor --heal` runs one bounded watchdog pass (public mode) for a unit that stays active but stops
  answering, which `Restart=always` never restarts: the bridge and the shim are probed on their local ports, cloudflared by its
  own `/ready` on the metrics port, and the public URL not at all. A unit is restarted after 3 failing passes in a row, at most 3
  times an hour and 10 minutes apart, never while the internet is unreachable, and two passes never overlap (a lock). Nothing
  schedules it; the README shows a user timer that runs it every two minutes (#6).

## 0.3.15

- `computer chatgpt doctor` prints its `chmod 600` and `chmod 755` fix hints with the path shell-quoted, so a hint for a data or
  config directory with a space or shell syntax such as `$(...)` in its path does what it says when pasted, and runs nothing
  else (#38).

## 0.3.14

- The write_pdf AppArmor check no longer crashes `computer mcp install`, `computer claude status` or `computer chatgpt doctor`
  when a profile includes a directory you cannot read: that include now gives no grant, so the check warns. It also reads a line
  with several rules (`userns, deny userns,`) rule by rule, so the deny wins; it no longer counts a child profile nested in an
  unrelated profile as covering Chrome; and `computer claude status` looks for Chrome under the HOME the Claude entry gives Desktop
  Commander (its `env.HOME`), not only under yours (#57).

## 0.3.13

- A ChatGPT token the shim never saw issued keeps its verified label: after a restart, a reinstall or a lost state directory,
  every ChatGPT call read `client=unverified` until ChatGPT's next token exchange, and a client registered before the shim's table
  existed was never verified at all. The shim now reads the client of an accepted token that its table cannot place from
  mcp-auth-proxy's own OAuth store (read only, without its lock, only the pages on the way to the token's keys; a rejected token
  never triggers a read). The shim unit gains `--oauth-db <data dir>/db`, so `computer chatgpt init` rewrites it (#37).

## 0.3.12

- `--cloudflare-cert` refuses, with exit 2 and before `cert.pem` is written, a certificate the pinned cloudflared would decode
  differently or not at all: a token that sets a field twice in any letter case (`apiToken` and `APITOKEN`; Go's encoding/json
  matches field names ignoring case and the last value wins, so cloudflared read an empty token and failed with "missing token"),
  an earlier value of the wrong type, a base64 body broken by a vertical tab, form feed or no-break space (Go's encoding/pem
  skips only spaces, tabs and line breaks), and a token that is not UTF-8 JSON (a byte-order mark, UTF-16, invalid bytes, NaN).
  Such a file used to be published as the login, so later runs skipped the browser and refused the right certificate as
  different (#33).
- `uninstall --purge` without a config now also stops and removes the units this tool wrote under another `--unit-prefix`,
  found by the marker line at the top of each unit file: before, only the default `computer-chatgpt-*` units were stopped, so a
  stack with a custom prefix kept running while its state was deleted. Every unit name of both modes under each such prefix is
  stopped (one whose file is gone may still run), and only the files this tool wrote are disabled and removed, since
  `systemctl disable` gives up on the whole list at the first unknown unit (#33).

## 0.3.11

- `computer mcp install` stopped by SIGTERM or SIGHUP (a closed terminal, a service manager) now stops npm and everything npm
  started before it exits, as Ctrl-C already did; it still dies of the same signal. Each npm ci runs in a staging directory of
  its own, so a retry never installs into a directory that an npm left running by a SIGKILLed install still writes to; such a
  directory is deleted by a later install once that npm has exited (#15).
- The browser download for PDF output is stopped with everything it started (puppeteer's unzip) when it times out or the install
  is interrupted, so nothing keeps writing the browser cache afterwards. It also gets npm's `node-options` setting as
  `NODE_OPTIONS` (in place of the caller's, as npm did when it ran the download): an npmrc's `node-options=--use-system-ca`
  behind a TLS-intercepting proxy works again. With no npm setting the caller's `NODE_OPTIONS` is kept (README, PDF output)
  (#16).

## 0.3.10

- Two machines with the same name can no longer end up serving one public hostname (which split ChatGPT's traffic between them
  and broke the other machine's connector with 401s). `install` and `login` now refuse, with exit 2 and a message naming what to
  do: adopting a tunnel by `--tunnel-name` while `cloudflared tunnel info` reports live connections for it (an answer without
  connection data is refused too, and credentials are fetched by the tunnel id that was checked); replacing the existing
  DNS record of a `--domain` hostname (`--yes` alone no longer does it; a `--hostname` record still follows `--yes`); and a local
  `<tunnel>.json` whose TunnelID is not the id Cloudflare lists for that name (the tunnel was created again elsewhere). The new
  `--force-takeover` flag passes the first two refusals, loudly, when you are sure no other machine serves the tunnel or hostname.
  This machine's own running tunnel, with matching credentials here (as `<tunnel name>.json`, or `<tunnel id>.json` as plain
  `cloudflared tunnel create` writes it, then copied to the name the units use), needs no override (#31).

## 0.3.9

- On Ubuntu 23.10+ (AppArmor's `kernel.apparmor_restrict_unprivileged_userns=1`), Desktop Commander's write_pdf fails with
  "No usable sandbox!" when the Chrome it launches, the Chrome for Testing build in its own cache, has no AppArmor profile that
  allows `userns,`. `computer mcp install` (and so the claude and chatgpt installs) now prints a warning naming that Chrome and
  the two fixes, both needing root: a profile for the downloaded build (the README has it) or the sysctl.
  `computer chatgpt doctor` shows the same as a `[warn]` check and `computer claude status` as a `[warn] pdf:` line; neither
  changes their exit code, since the shell and file tools are not affected. Nothing changes system settings (#14).

## 0.3.8

- The ChatGPT shim no longer holds request bodies in memory before mcp-auth-proxy has checked the login: it read each body whole
  (up to 32 MiB) before forwarding it, with one thread per connection and no limit, so 64 unauthenticated uploads that held back
  their last bytes kept about 2 GiB. Bodies now go to the proxy as they arrive (64 KiB at a time, plus the small copy the access
  log and the client label read). When the proxy answers before the body is through (its 401 to an unauthenticated upload),
  also while the client pauses mid-body, the shim stops sending and passes that answer on at once. A client that leaves
  mid-body is never forwarded as a complete request. At most 128 connections are served at once: more get a 503, with one
  journal line per minute (#29).

## 0.3.7

- `computer claude status` no longer reports a broken autostart entry as healthy: an empty file or one without a
  `[Desktop Entry]` section is `invalid`, and an entry whose Exec is missing or names a program that does not exist is
  `broken-exec`; both are FAIL (exit 3). `computer claude install` repairs both after a backup: the empty or section-less file is
  replaced, and a broken Exec line alone is replaced, keeping your other keys. The Exec line it writes is quoted and escaped per
  the Desktop Entry spec, so a launcher path with a space, `"`, `$`, a backtick, a backslash or `%` survives (#10).

## 0.3.6

- `computer chatgpt connect` over ssh (no `DISPLAY` or `WAYLAND_DISPLAY`) now prints the ChatGPT settings URL to open yourself.
  It used its own copy of the browser opener that skipped the display check, so it ran `xdg-open` with nothing to show it on and
  never printed the URL; it now uses the same opener as `computer chatgpt login` (#32).
- Correction to the 0.2.2 entry: Desktop Commander waits on its welcome page not only on its first run in a HOME but until that
  page has been opened once there; `--no-onboarding` covers both.

## 0.3.5

- `computer chatgpt login` no longer exits 0 when you decline to replace an existing DNS record of the hostname: the record still
  points elsewhere, so ChatGPT would not reach this PC. login now exits 2 with the same "still points somewhere else" error as
  `install`, naming `--yes` as the fix; `--skip-dns` still succeeds (#22).

## 0.3.4

- A path or name in the chatgpt config that contains U+007F (DEL) or another control character no longer breaks every later
  `computer chatgpt` command: config.toml wrote it raw, which TOML forbids, so loading the config failed. Such characters are now
  written as `\uXXXX` escapes, and the file is always read and written as UTF-8 whatever the locale (#23).

## 0.3.3

- The MCP handshake probe (`computer claude status`) reports a server that exits before answering as `server exited before
  answering (exit N)` every time. Which of our write or our read first noticed the exit was a race, and the write side said
  `server closed its input (exit None)` instead; both sides now wait for the exit and quote its real status, and a server that
  closes its pipes but keeps running is reported as a timeout (#19).

## 0.3.2

- `computer chatgpt doctor` fails `<binary> installed` when the binary has the pinned digest but you cannot execute it (a
  backup restore that dropped the exec bit leaves systemd unable to start the unit while doctor said all was well), and says
  to run `computer chatgpt init`; init now repairs such a binary to mode 0755 instead of leaving it (#30).
- The fix hint of doctor's `proxy answers` check names the bridge unit (`<prefix>-bridge.service`), where the proxy runs; it
  named a `<prefix>-proxy.service` that does not exist (#20).
## 0.3.1

- Stopping or restarting the ChatGPT bridge unit no longer marks it failed: `computer chatgpt quiet` returned the stopped
  mcp-auth-proxy's `-15` as its own exit status (exit 241), which systemd records as `Result=exit-code` (and answers with any
  `OnFailure=` a user drop-in adds). quiet now dies of the same signal as the command it runs, so a stop by SIGTERM is clean; a
  crash signal that writes a core (SIGSEGV, SIGABRT, ...) or one it cannot re-raise becomes exit 128+N, so a crash of the
  command never makes quiet dump a core of its own (#25).

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
