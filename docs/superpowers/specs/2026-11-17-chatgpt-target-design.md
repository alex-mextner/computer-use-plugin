# ChatGPT target: design

## Goal

`computer chatgpt install --hostname <name>` gives ChatGPT a shell and files on this PC in ONE command, safely re-runnable, reusing the
shared core of the Claude Desktop design (`2026-11-11-claude-desktop-support-design.md`) for Desktop Commander.

## Why an HTTP gateway here

ChatGPT connects only to remote MCP servers over public HTTPS (or OpenAI's Secure MCP Tunnel, kept as an experimental mode). So unlike
Claude Desktop (stdio, no daemon) this target runs an always-on chain as systemd user units:

```
ChatGPT -> Cloudflare named tunnel -> shim 127.0.0.1:8767 -> mcp-auth-proxy 127.0.0.1:8766 -> computer mcp serve (stdio)
```

- cloudflared: outbound-only tunnel, no open port; its certificate, credentials and `config.yml` live in the target's own directory
  (`~/.config/computer/chatgpt/cloudflared`), passed as `--config`, `--origincert`, `--credentials-file`. `~/.cloudflared` may belong to
  another tunnel on the machine and is never used unless the user points `--cloudflared-dir` at it. `cloudflared tunnel login` has no
  output flag, so it runs with HOME set to a staging directory (and an empty PATH so it cannot start a browser under that HOME); the
  URL it prints is opened with the real environment and the certificate is moved into place (0600).
- mcp-auth-proxy (sigbit, pinned v2.10.2): OAuth 2.1, dynamic client registration, PKCE, password login. Password generated locally
  into a 0600 env file, read by systemd `EnvironmentFile=`, never printed or in an argv. Its stdio child is the absolute
  `computer mcp serve`; a custom `XDG_DATA_HOME` travels in the unit's environment.
- shim (`computer chatgpt shim`): `WWW-Authenticate: Bearer resource_metadata=...` on 401s, the path-suffix protected-resource
  document, an SSE comment on idle streams, and the access log with a verified client label.
- Units: `Restart=always`, `StartLimitIntervalSec=0`, `WantedBy=default.target`, lingering enabled.

## The one command

1. Prerequisites before any write: pinned builds for this CPU, node, npm, loginctl (127), the user manager (127), the `computer`
   executable, the hostname's zone on Cloudflare (DoH NS lookup: 2 when it is elsewhere, a warning when the lookup fails),
   loopback ports free or already ours (2), a `--cloudflare-cert` that reads as an account certificate.
2. `init` building block without the config: password, `computer mcp install`, pinned downloads.
3. `login` building block: login when no certificate, create the tunnel (an existing one without local credentials only when
   named with `--tunnel-name`: its credentials are fetched with `tunnel token --cred-file`; otherwise exit 2), `route dns`.
   cloudflared reports "already configured to route to your tunnel" for our own record; a record that points elsewhere fails
   with "already exists" and is replaced (`--overwrite-dns`) only after consent (`--yes`).
4. config.toml and the unit files (only after `login` succeeded: a run stopped there leaves both unchanged, so the other
   commands still describe the running stack and the corrected re-run still restarts the running units), then the `start`
   building block: lingering, `enable --now`, restart of units whose file changed, wait for the local chain.
5. Wait for the public URL to answer 401 with the challenge (`--timeout`, exit 7 after printing the fields when it does not).
6. Print ChatGPT's connector fields and the password file location.

## Decisions

- Default tunnel name `computer-<hostname with dashes>`: one tunnel per public hostname, stable across re-runs. Superseded in 0.3.0
  (issue #18): the default is the machine name, and `--domain` derives the hostname `<machine>-mcp.<zone>` (README "Several
  machines").
- Default ports stay 8766/8767/20241 so an existing hand-made stack can be adopted (CUTOVER.md); the port check refuses to fight a
  foreign process for them.
- The pins of all release binaries live in `computerlib/pins.py`, the one pin table.
- `uninstall` never deletes the Cloudflare tunnel, its DNS record or the shared Desktop Commander; it says what it kept.
- The tunnel-client mode (issue #3) is carried over unchanged in behaviour, now also launching `computer mcp serve`.

## Testing

Hermetic: fake systemctl/loginctl/node/npm executables, a fake `cloudflared` with the real flag shapes and messages (its state is the
"account"), stand-in DoH answers, stand-in public URL check. Opt-in integration: the real mcp-auth-proxy with `computer mcp serve` ->
stub MCP server behind it (full OAuth flow, initialize, tools/list, tools/call), and the real tunnel-client against a stand-in control
plane.
