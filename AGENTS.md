# AGENTS.md — chatgpt-computer-use-plugin

`computer` is a Python CLI that gives a chat client a shell and files on this PC through an MCP server (Desktop Commander).
Targets today: `mcp` (the client-neutral core: pinned Desktop Commander install and stdio launcher) and `claude` (the Claude Desktop
adapter). `computer <target> <command>`; the tree is built to take more client adapters (a ChatGPT one is planned, not built).

Python-only (agent-tools "Python-only tools" rule): Desktop Commander (npm), mcp-auth-proxy (Go), cloudflared and tunnel-client are
external pinned processes this tool installs and drives, never source in this repo. Shell glue over 40 lines is not allowed.

## Layout (flat, not src) and why

```
computerlib/            import package; dist name `hyper-computer`, command `computer`
  cli.py                build_parser()/main(argv)->int, --version from package metadata
  commandtree.py        Command + command_target(): a target package builds its TARGET from its commands/ modules
  desktop_commander.py  managed pinned install (install.json, flock, npm --prefix) and launch_spec; pins.py is the one pin table
  mcp_probe.py          real MCP handshake used by every adapter's status
  targets/<name>/       one package per target, exposes TARGET (self-registered, no registry to edit)
    <name>/commands/    one module per command exposing COMMAND (mcp: install, serve; claude: install, status, uninstall)
tests/                  pytest, hermetic
```

Flat layout with an explicit `[tool.setuptools.packages.find]` is what every sibling CLI uses (harness-cli, keychain-cli, qa-cli);
the dist is `hyper-<name>` with a different command name, like `hyper-qa` -> `qa`. Setuptools, not hatch/uv_build, for the same reason.
`computerlib` is target-neutral so `computer claude ...` needs no rename.

## Conventions this repo follows

- argparse only (the three agent-tools libs it uses are argparse-shaped). Heavy imports lazily inside commands.
- Exit codes via `agenttools_errors` (`guard`, `AgentToolError(what=, why=, fix=)`); listed in `--help`.
- agent-tools libs are consumed as direct git URLs pinned to ONE SHA in `pyproject.toml` (no registry serves them and their own
  agent-tools deps do not resolve transitively, so every needed dist is listed). This is the first declared git dependency in the
  ecosystem. CI runs `git ls-remote https://git.hyperide.ai/ultrabricks/agent-tools.git` to prove the host is reachable from the runner.
- Utility code useful to other tools belongs in agent-tools/lib via its own ticket/PR (never fork a lib here). Target-specific code
  (ChatGPT dialog helper, the OAuth shim rules, the pin table) lives here.
- Never print or log passwords, tokens, cookies or keys. The password lives in a 0600 file (keychain-cli is the preferred store).
- Version: `[project] version`, bumped on every shippable change (feat=minor, fix=patch). `--version` reads package metadata.
- Comments say why, not what changed. File-header docstring on non-trivial modules.
- Help text and README/docs change in the same commit as the flag they describe; a test keeps them in sync.

## Workflow

Ticket (GitHub issue of this repo, prefix `CCU`, bare `#NNN`) -> `rig worktree create <name> --from origin/main` (never author in
the primary checkout) -> tests -> `review diff --staged --task <N>` `[ok]` -> PR -> `gh ship <PR>`. Conventional commits, one logical
change each. Gates: `uv run --group dev ruff check . && uv run --group dev pyright && uv run --group dev pytest -q` and
`gitleaks dir . --redact --no-banner --exit-code 1`. The repo is private so Actions minutes are blocked: `.ship-config` carries the
local gate `gh ship` runs when CI is structurally down.

## Tests

pytest, hermetic: an autouse fixture points HOME/XDG at tmp_path, removes credentials env and blocks outgoing network. Fakes over mocks
(fake `systemctl`, fake downloads, stub stdio MCP server). Never test against the real Cloudflare hostname or the live user units.
`integration` tests (opt-in, `COMPUTER_INTEGRATION=1`) run the real mcp-auth-proxy binary on scratch ports.

## Live system safety

The user's running stack (units `dc-mcp-*`, ports 8766/8767, public https://dc-mcp.mextner.com/mcp) is NOT to be touched by
development of this repo: use scratch HOME and other ports. The cutover is documented in CUTOVER.md.

# >>> rig-managed orchestrator (do not edit) >>>
# Orchestrator, not worker. Do not implement inline. Do not run `gh` except an unchained
# `gh ship <N>`, or read-only `gh issue` list/view, `gh pr` list/view/checks/diff, `gh run`
# list/view/watch, `gh repo view`, GET-only `gh api` (no -f/-F/--input). Do not `git
# worktree add` — `rig worktree create`.
# Read-only (`git status`, `ls`, `cat`, `grep`, `git worktree list`) is fine. Dispatch a subagent for the rest. Skill: delegate-work-to-subagents. Hook: orchestrator-stays-thin.
# Key reports MUST go to Telegram (`tg --tag report`): quote the user's original words, include ticket/PR URLs, name the measure already started, draft via `review just-ask`. Never "not in prod" with no next action.
# After a successful `gh ship` of a load-bearing feature, merge is not done: (1) turn it on
# in settings if it has a config knob, (2) `rig apply commit` on this machine, (3) prove it
# WORKS on this machine (command output, Telegram, deny text — SHA/on-disk is not a proof), (4) re-check the original user quote is solved.
# A helper yield is not done — do not tick the todo until the original ask is live-proven; wake parked helpers via hub send.
# <<< rig-managed orchestrator (do not edit) <<<

# >>> rig-managed autonomous-mode (do not edit) >>>
# Mode autonomous: extra-approval gate OFF. Do not invent a hold-for-human.
# Ship when gates pass. Skill: autonomous-mode (skill://autonomous-mode).
# <<< rig-managed autonomous-mode (do not edit) <<<
