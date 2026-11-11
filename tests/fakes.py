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
    """A `node` that answers `-p` with its exec path (default itself) and version, and is otherwise the stub MCP server."""
    path = directory / "node"
    reported = exec_path if exec_path is not None else path
    return write_executable(
        path,
        f'#!/bin/sh\nif [ "$1" = "-p" ]; then echo "{reported}"; echo "{version}"; exit 0; fi\n'
        f'exec "{sys.executable}" "{STUB_SERVER}" "$@" {" ".join(stub_args)}\n',
    )


def fake_npm(
    directory: Path, *, mode: Literal["ok", "fail", "no_files", "hang"] = "ok", fail_with: str = "", side_effect: str = ""
) -> Path:
    """An `npm` that logs its argv and lays out the package files `npm install --prefix <dir> pkg@ver` would.

    `fail` prints `fail_with` on stderr and exits 1; `no_files` exits 0 without installing; `hang` never returns. `side_effect` is
    a shell snippet run first (something else touching the machine while npm is busy).
    """
    if mode == "fail":
        body = f'#!/bin/sh\necho "{fail_with}" >&2\nexit 1\n'
    elif mode == "no_files":
        body = "#!/bin/sh\nexit 0\n"
    elif mode == "hang":
        body = "#!/bin/sh\nexec /bin/sleep 60\n"
    else:
        body = (
            "#!/bin/sh\n"
            f'echo "$@" >> "{directory}/npm.log"\n'
            'while [ $# -gt 0 ]; do if [ "$1" = "--prefix" ]; then prefix="$2"; fi; shift; done\n'
            'pkg="$prefix/node_modules/@wonderwhy-er/desktop-commander"\n'
            'mkdir -p "$pkg/dist" && echo "// fake dc" > "$pkg/dist/index.js"\n'
        )
    return write_executable(directory / "npm", body.replace("#!/bin/sh\n", f"#!/bin/sh\n{side_effect}\n", 1))


def fake_computer(directory: Path, *stub_args: str) -> Path:
    """An executable `computer` that is the stub MCP server; argv shows up as serverInfo.version."""
    body = f'#!/bin/sh\nexec "{sys.executable}" "{STUB_SERVER}" "$@" {" ".join(stub_args)}\n'
    return write_executable(directory / "computer", body)


def minimal_env(home: Path, **extra: str) -> dict[str, str]:
    """What an autostarted desktop app gives its children: HOME and a bare PATH, nothing else."""
    env = {"HOME": str(home), "PATH": MINIMAL_PATH}
    env.update(extra)
    return env
