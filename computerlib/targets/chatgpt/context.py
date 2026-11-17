"""What every command needs: the settings, the layout derived from them and the systemd wrapper."""

from __future__ import annotations

from dataclasses import dataclass

from agenttools_errors import UsageError

from computerlib.targets.chatgpt.settings import Layout, Settings, config_file, load_settings
from computerlib.targets.chatgpt.systemd import Systemd


@dataclass(frozen=True)
class Context:
    settings: Settings
    layout: Layout
    systemd: Systemd


def load_context(*, configured: bool = True) -> Context:
    """The context of an initialised target; `configured=False` also accepts a machine that has not run `init` yet."""
    if configured and not config_file().exists():
        raise UsageError(
            what="computer chatgpt is not set up on this machine",
            why=f"there is no {config_file()}",
            fix="run `computer chatgpt init --hostname <public hostname>`",
        )
    settings = load_settings()
    layout = Layout.of(settings)
    return Context(settings, layout, Systemd(layout.unit_dir))
