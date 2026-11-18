# Cutover: from the `.files` dc-mcp module to `computer chatgpt`

For the machine that already runs the stack from `~/.files/ubuntu/modules/dc-mcp` (work package C, done after this release is
tagged). Goal: the same public URL, the same OAuth state and the same unit names, with the new CLI owning units, shim, pins and logs.
Nothing here is automated by this repo; it is the checklist for the person (or agent) doing it.

## What stays, what moves

| Thing | Live value | In the new CLI |
|---|---|---|
| Units | `dc-mcp-bridge`, `dc-mcp-shim`, `dc-mcp-tunnel` (`Restart=always`, `StartLimitIntervalSec=0`) | `unit_prefix = "dc-mcp"` gives the same three names; unit text is rewritten by `init` |
| Ports | proxy 8766, shim 8767, cloudflared metrics 20241 (auto-picked today) | `proxy_port`, `shim_port`, `metrics_port` (pinned with `--metrics`) |
| Hostname / tunnel | `dc-mcp.mextner.com`, tunnel `dc-local` | `hostname`, `tunnel_name`; cloudflared state in `cloudflared_dir` (default `~/.config/computer/chatgpt/cloudflared`): copy `~/.cloudflared/cert.pem` there and the tunnel's `<tunnel id>.json` as `dc-local.json` (mode 600); `~/.cloudflared` itself is never touched |
| OAuth state | `~/.local/share/mcp-auth-proxy` (keys, BoltDB: losing it makes ChatGPT re-authorize) | `data_dir`, kept as is |
| Access log + client table | `~/.local/state/dc-mcp/access.log`, `clients.json` | `state_dir`; same file names and line format, so history carries over |
| Password | `~/.config/dc-mcp-auth.env` (`PASSWORD=`, mode 600) | `password_file`, kept as is (no new password) |
| Binaries | `mcp-auth-proxy v2.10.2`, `cloudflared 2026.10.7` in `~/.local/bin` (digests equal the pin table) | downloaded again into `~/.local/share/computer/chatgpt/bin` and verified; the old copies stay until `.files` drops them |
| Desktop Commander | `npx -y @wonderwhy-er/desktop-commander@latest` on every start | the shared core: pinned 0.3.2 via `computer mcp install`, run by the bridge as `computer mcp serve`, no `npx` |
| Node shim | `~/.local/share/dc-mcp-shim/shim.mjs` | replaced by `computer chatgpt shim` (same rules, same log format) |
| `dc-mcp-quiet`, `dc-mcp-log` | shell scripts | `computer chatgpt quiet`, `computer chatgpt logs` |
| Tool-log pruning timer | `dc-mcp-tool-log-prune.{service,timer}` | not part of this release; leave it in place until a pruner lands in agent-tools |

## Steps

1. Install the release: `uv tool install "hyper-computer @ git+https://github.com/alex-mextner/computer-use-plugin.git@<release SHA>"`.
2. Write the config by hand, BEFORE `init`, so `init` adopts the live layout (`~/.config/computer/chatgpt/config.toml`):

   ```toml
   mode = "public"
   scheme = "https"
   hostname = "dc-mcp.mextner.com"
   tunnel_name = "dc-local"
   proxy_port = 8766
   shim_port = 8767
   metrics_port = 20241
   unit_prefix = "dc-mcp"
   state_dir = "~/.local/state/dc-mcp"
   data_dir = "~/.local/share/mcp-auth-proxy"
   password_file = "~/.config/dc-mcp-auth.env"
   ```
3. `computer chatgpt init` (downloads and verifies the pins, writes the three unit files, reloads systemd; it starts nothing). Check
   `computer chatgpt doctor --local`: only "active" checks may differ from a fresh install.
4. Switch one unit at a time, same ports, proxy data untouched, tunnel last:
   `systemctl --user restart dc-mcp-bridge` and wait until `computer chatgpt status` shows the proxy answering; then `dc-mcp-shim`;
   then `dc-mcp-tunnel`.
5. Verify (all of it, from this machine):
   - `systemctl --user is-active dc-mcp-bridge dc-mcp-shim dc-mcp-tunnel dc-mcp-tool-log-prune.timer`
   - `curl -si -X POST https://dc-mcp.mextner.com/mcp -d '{}'` is 401 with `www-authenticate: Bearer resource_metadata=...`
   - `computer chatgpt doctor` is all `[ok]` including the public URL and the cloudflared `/ready` check
   - the OAuth flow still works with the existing ChatGPT connector (no re-authorisation: the data dir is unchanged); a tool call from
     ChatGPT shows `client=openai-mcp rpc=tools/call` in `computer chatgpt logs --client openai-mcp` (a line from before the restart
     reads `unverified` until that client's next token exchange, up to a day)
   - recovery: `kill <pid of the shim>` and `<pid of cloudflared>`, each comes back within RestartSec; `status` is healthy again
   - rotation: the access log rolls over at its size cap (`access.log.1`)
   - the systemd-in-Docker boot test (image `ut-verify-systemd:26.04`, own container name, no Cloudflare credentials) once the
     harness from agent-tools is available
6. In `.files`: replace the module with a thin installer (`packages.toml` `uv` entry pinned to the tag or SHA, `units.toml` keeps the
   names, desktop launcher and icon stay), delete the Node shim, the three units' hand-written text, `dc-mcp-quiet`, `dc-mcp-log`,
   the old tests and scripts, in one PR. No compatibility shims.

## Rollback

The old unit files are in `.files` and the OAuth state was never touched: restore the three unit files from `.files`, `systemctl
--user daemon-reload`, restart bridge, shim, tunnel in that order. Keep the old shim and units until step 5 passes in full.

## Not covered by this release

The bounded watchdog (`doctor --heal`) arrives in a later release. `computer chatgpt login` (Cloudflare) and the experimental `tunnel` mode
exist since 0.2.0 but are not part of this cutover: an existing tunnel and its credentials are used as they are, and the mode stays `public`.
