"""Fakes shared by the tests: executable scripts standing in for node, npm and the `computer` launcher."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Literal

STUB_SERVER = Path(__file__).with_name("stub_mcp_server.py")
MINIMAL_PATH = "/usr/bin:/bin"


def write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def fake_node(directory: Path, *stub_args: str, exec_path: Path | None = None, version: str = "24.0.0") -> Path:
    """A `node` that answers `-p` with its exec path (default itself) and version, logs a run of puppeteer's `install.mjs` to
    `node.log` next to itself (exit `$FAKE_BROWSER_EXIT`, default 0), answers the browser check (`-e`) with 1 when
    `$FAKE_BROWSER_MISSING` is set, and is otherwise the stub MCP server."""
    path = directory / "node"
    reported = exec_path if exec_path is not None else path
    return write_executable(
        path,
        '#!/bin/sh\ncase "$1 $2" in "-p "*process.platform*) echo "linux x64"; exit 0;; esac\n'
        f'if [ "$1" = "-p" ]; then echo "{reported}"; echo "{version}"; exit 0; fi\n'
        f'case "$1" in */install.mjs) echo "$PWD $@" >> "{directory}/node.log"; '
        'echo "browser download failed" >&2; exit "${FAKE_BROWSER_EXIT:-0}";; esac\n'
        'if [ "$1" = "-e" ]; then [ -z "$FAKE_BROWSER_MISSING" ]; exit $?; fi\n'
        f'exec "{sys.executable}" "{STUB_SERVER}" "$@" {" ".join(stub_args)}\n',
    )


def fake_npm(
    directory: Path, *, mode: Literal["ok", "fail", "no_files", "hang"] = "ok", fail_with: str = "", side_effect: str = ""
) -> Path:
    """An `npm` that, like `npm ci`, first deletes the prefix's node_modules, then logs its argv, keeps the package-lock.json it
    found in the prefix as `npm-lock.json` next to itself, and lays out the package files `npm ci --prefix <dir>` would.

    `fail` prints `fail_with` on stderr and exits 1; `no_files` exits 0 without installing; `hang` never returns. `side_effect` is
    a shell snippet run first (something else touching the machine while npm is busy).
    """
    start = (
        f"#!/bin/sh\n{side_effect}\n"
        'os="${npm_config_os:-linux}"; cpu="${npm_config_cpu:-x64}"\n'
        'for arg in "$@"; do if [ "$take" = 1 ]; then prefix="$arg"; fi; take=0; [ "$arg" = "--prefix" ] && take=1\n'
        '  case "$arg" in --os=*) os="${arg#--os=}";; --cpu=*) cpu="${arg#--cpu=}";; esac; done\n'
        'if [ -n "$prefix" ]; then rm -rf "$prefix/node_modules"; fi\n'
    )
    if mode == "fail":
        body = f'echo "{fail_with}" >&2\nexit 1\n'
    elif mode == "no_files":
        body = "exit 0\n"
    elif mode == "hang":
        body = "exec /bin/sleep 60\n"
    else:
        body = (
            f'echo "$@" >> "{directory}/npm.log"\n'
            f'cp "$prefix/package-lock.json" "{directory}/npm-lock.json" || exit 1\n'
            'pkg="$prefix/node_modules/@wonderwhy-er/desktop-commander"\n'
            'mkdir -p "$pkg/dist" && echo "// fake dc" > "$pkg/dist/index.js"\n'
            'rg="$prefix/node_modules/@vscode/ripgrep-$os-$cpu/bin"\n'
            'if [ -z "$FAKE_NPM_SKIP_RIPGREP" ]; then mkdir -p "$rg" && touch "$rg/rg"; fi\n'
        )
    return write_executable(directory / "npm", start + body)


def fake_computer(directory: Path, *stub_args: str) -> Path:
    """An executable `computer` that is the stub MCP server; argv shows up as serverInfo.version."""
    body = f'#!/bin/sh\nexec "{sys.executable}" "{STUB_SERVER}" "$@" {" ".join(stub_args)}\n'
    return write_executable(directory / "computer", body)


def minimal_env(home: Path, **extra: str) -> dict[str, str]:
    """What an autostarted desktop app gives its children: HOME and a bare PATH, nothing else."""
    env = {"HOME": str(home), "PATH": MINIMAL_PATH}
    env.update(extra)
    return env
