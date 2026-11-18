"""Settings of the chatgpt target (non-secret, TOML) and the file layout derived from them.

WHAT: `Settings` is what the user chose (mode, hostname, ports, optional path overrides); `Layout` is where everything lives.
HOW IT IS REACHED: every command calls `load_settings()` then `Layout.of(settings)`.
INVARIANTS: the environment (HOME, XDG_*) is read on every call, never cached, so tests can scratch it. Secrets never live here:
the login password is in `Layout.password_file` and the tunnel-mode runtime key in `Layout.tunnel_key_file` (both 0600) only.
The default mode is `public` and nothing in this module changes it.
The overrides exist so a machine that already runs the stack under other names can adopt it without moving state (CUTOVER.md).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from agenttools_errors import ConfigError

MODES = ("public", "tunnel")
DEFAULT_MODE = "public"
UNIT_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Settings:
    mode: str = DEFAULT_MODE
    scheme: str = "https"  # http only for local development (`run` on a scratch port); ChatGPT needs https
    hostname: str = ""
    tunnel_name: str = ""
    proxy_port: int = 8766
    shim_port: int = 8767
    metrics_port: int = 20241  # cloudflared readiness endpoint (127.0.0.1:<port>/ready)
    tunnel_id: str = ""  # tunnel mode: the OpenAI tunnel this machine serves ("tunnel_..."); never shared with another daemon
    tunnel_health_port: int = (
        8794  # tunnel mode: tunnel-client's loopback health/metrics endpoint (8793 is the guardian tunnel's)
    )
    unit_prefix: str = "computer-chatgpt"
    state_dir: str = ""
    data_dir: str = ""
    install_dir: str = ""
    password_file: str = ""
    unit_dir: str = ""
    # cloudflared's own state for this target (account certificate, tunnel credentials, config.yml). Never ~/.cloudflared by
    # default: that directory may belong to another tunnel on this machine; point here explicitly to reuse it.
    cloudflared_dir: str = ""


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else _home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "computer" / "chatgpt"


def config_file() -> Path:
    return config_dir() / "config.toml"


@dataclass(frozen=True)
class Layout:
    """Absolute paths of everything the target owns."""

    config_dir: Path
    state_dir: Path  # access log, clients table, checklist state
    data_dir: Path  # mcp-auth-proxy OAuth keys and database (losing it invalidates every issued token)
    install_dir: Path  # pinned binaries
    password_file: Path  # PASSWORD=... for mcp-auth-proxy, mode 0600
    tunnel_key_file: Path  # tunnel mode: the runtime API key (Tunnels Read+Use) for tunnel-client, mode 0600
    unit_dir: Path
    cloudflared_dir: Path  # cert.pem, <tunnel name>.json credentials and config.yml of the Cloudflare tunnel, mode 0700

    @property
    def bin_dir(self) -> Path:
        return self.install_dir / "bin"

    @property
    def tunnel_client_binary(self) -> Path:
        return self.bin_dir / "tunnel-client"

    @property
    def access_log(self) -> Path:
        return self.state_dir / "access.log"

    @property
    def clients_file(self) -> Path:
        return self.state_dir / "clients.json"

    @property
    def origin_cert(self) -> Path:
        """The Cloudflare account certificate `cloudflared tunnel login` produces (lets it create tunnels and DNS routes)."""
        return self.cloudflared_dir / "cert.pem"

    @property
    def cloudflared_config(self) -> Path:
        """Passed as --config to every cloudflared call, so it never picks up ~/.cloudflared/config.yml of another tunnel."""
        return self.cloudflared_dir / "config.yml"

    def tunnel_credentials(self, tunnel_name: str) -> Path:
        return self.cloudflared_dir / f"{tunnel_name}.json"

    @staticmethod
    def of(settings: Settings) -> Layout:
        def chosen(value: str, default: Path) -> Path:
            return Path(value).expanduser() if value else default

        cfg = config_dir()
        return Layout(
            config_dir=cfg,
            state_dir=chosen(settings.state_dir, _xdg("XDG_STATE_HOME", ".local/state") / "computer" / "chatgpt"),
            data_dir=chosen(settings.data_dir, _xdg("XDG_DATA_HOME", ".local/share") / "computer" / "chatgpt" / "oauth"),
            install_dir=chosen(settings.install_dir, _xdg("XDG_DATA_HOME", ".local/share") / "computer" / "chatgpt"),
            password_file=chosen(settings.password_file, cfg / "auth.env"),
            tunnel_key_file=cfg / "tunnel.key",
            unit_dir=chosen(settings.unit_dir, _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"),
            cloudflared_dir=chosen(settings.cloudflared_dir, cfg / "cloudflared"),
        )


def validate(settings: Settings) -> Settings:
    if settings.mode not in MODES:
        raise ConfigError(
            what=f"unknown mode {settings.mode!r}",
            why=f"the chatgpt target has the modes {', '.join(MODES)}",
            fix="use --mode public or --mode tunnel (or edit config.toml)",
        )
    if settings.scheme not in ("https", "http"):
        raise ConfigError(what=f"unknown scheme {settings.scheme!r}", why="the public URL is http or https", fix="use https")
    for name in ("proxy_port", "shim_port", "metrics_port", "tunnel_health_port"):
        port = getattr(settings, name)
        if not 1 <= port <= 65535:
            raise ConfigError(what=f"{name} {port} is not a TCP port", why="ports are 1-65535", fix=f"set {name} in config.toml")
    if settings.proxy_port == settings.shim_port:
        raise ConfigError(
            what="proxy_port and shim_port are the same",
            why="the shim listens on one loopback port and proxies to the other",
            fix="give them different ports",
        )
    if not UNIT_PREFIX.match(settings.unit_prefix):
        raise ConfigError(
            what=f"unit prefix {settings.unit_prefix!r} cannot name a systemd unit",
            why="a unit name is letters, digits, '_', '.' and '-'",
            fix="pass --unit-prefix with only those characters (or edit config.toml)",
        )
    return settings


def parse_settings(text: str) -> Settings:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(what="config.toml is not valid TOML", why=str(error), fix=f"fix or delete {config_file()}") from error
    known = {f.name: f.type for f in fields(Settings)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(
            what=f"unknown setting(s) {', '.join(unknown)} in config.toml",
            why=f"known settings: {', '.join(known)}",
            fix=f"remove them from {config_file()}",
        )
    for key, value in raw.items():
        if type(value).__name__ != known[key]:
            raise ConfigError(
                what=f"setting {key} must be of type {known[key]}", why=f"got {value!r}", fix=f"fix {config_file()}"
            )
    return validate(Settings(**raw))


def load_settings() -> Settings:
    path = config_file()
    return parse_settings(path.read_text()) if path.exists() else Settings()


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_settings(settings: Settings) -> str:
    lines = ["# computer chatgpt: non-secret settings (the login password is not here)."]
    for key, value in asdict(settings).items():
        lines.append(f"{key} = {_quote(value) if isinstance(value, str) else value}")
    return "\n".join(lines) + "\n"


def save_settings(settings: Settings) -> Path:
    validate(settings)
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(render_settings(settings))
    temp.replace(path)
    return path
