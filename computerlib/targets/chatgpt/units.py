"""systemd --user unit text for the stack processes.

WHAT: `render_unit` turns a `stack.Process` into a unit file; `unit_name` is the one place unit names are made.
INVARIANTS: Restart=always with StartLimitIntervalSec=0 so a crash loop or a network that is down at boot can never exhaust
systemd's start limit and leave a unit dead for good (it keeps retrying every RestartSec until the cause is gone); no
network-online.target ordering (it does not exist in the user manager); ordering/wants only between units of this stack.
NOTE: a generic unit renderer with these fields belongs in agenttools_service (extension planned); this is the minimum until it
ships.
"""

from __future__ import annotations

import re

from computerlib.targets.chatgpt.stack import Process

MARKER = "# Managed by `computer chatgpt`: edit config.toml and re-run `computer chatgpt init`, not this file."
_PLAIN = re.compile(r"^[\w@+=:,./-]+$")


def unit_name(prefix: str, service: str) -> str:
    return f"{prefix}-{service}.service"


def exec_quote(argument: str) -> str:
    """One ExecStart word: systemd expands % and $ everywhere and splits on whitespace, so escape both and quote when needed."""
    if _PLAIN.match(argument):
        return argument
    escaped = argument.replace("%", "%%").replace("$", "$$")
    return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _env_assignment(name: str, value: str) -> str:
    quoted = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{name}={quoted}"'


def render_unit(process: Process, prefix: str) -> str:
    lines = [MARKER, "[Unit]", f"Description={process.description}"]
    if process.after:
        other = unit_name(prefix, process.after)
        lines += [f"After={other}", f"Wants={other}"]
    lines += ["StartLimitIntervalSec=0", "", "[Service]"]
    lines += [_env_assignment(name, value) for name, value in process.env]
    lines += [f"EnvironmentFile={path}" for path in process.env_files]
    lines += [
        "ExecStart=" + " ".join(exec_quote(word) for word in process.argv),
        "Restart=always",
        f"RestartSec={process.restart_seconds}",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"
