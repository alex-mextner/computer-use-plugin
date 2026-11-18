"""The comparison of the two connection modes, rendered from data (English and Russian).

`public`: Cloudflare named tunnel + mcp-auth-proxy OAuth + our shim. `tunnel`: OpenAI Secure MCP Tunnel (tunnel-client).
The facts are from the research of 2026-11-08; "proven here" means a real ChatGPT tool call was seen in the access log on the
author's PC, and nothing else counts as proof. The default is `public` and detection of tunnel prerequisites never changes it.
"""

from __future__ import annotations

from dataclasses import dataclass

LANGUAGES = ("en", "ru")


@dataclass(frozen=True)
class Row:
    label: dict[str, str]
    public: dict[str, str]
    tunnel: dict[str, str]


def _row(label: tuple[str, str], public: tuple[str, str], tunnel: tuple[str, str]) -> Row:
    return Row(
        dict(zip(LANGUAGES, label, strict=True)),
        dict(zip(LANGUAGES, public, strict=True)),
        dict(zip(LANGUAGES, tunnel, strict=True)),
    )


ROWS: tuple[Row, ...] = (
    _row(("needs a domain + Cloudflare", "нужны домен и Cloudflare"), ("yes", "да"), ("no", "нет")),
    _row(
        ("needs an OpenAI Platform key", "нужен ключ OpenAI Platform"),
        ("no", "нет"),
        ("yes (Tunnels Read+Use)", "да (Tunnels Read+Use)"),
    ),
    _row(("URL on the internet", "адрес в интернете"), ("yes (OAuth login)", "да (вход по паролю, OAuth)"), ("no", "нет")),
    _row(
        ("processes", "процессы"),
        ("4 (proxy, shim, cloudflared, DC)", "4 (proxy, shim, cloudflared, DC)"),
        ("2 (tunnel-client, DC)", "2 (tunnel-client, DC)"),
    ),
    _row(("ChatGPT app must be open", "приложение ChatGPT должно быть открыто"), ("no", "нет"), ("no", "нет")),
    _row(("works while the PC sleeps", "работает, пока ПК спит"), ("no", "нет"), ("no", "нет")),
    _row(
        ("request time limit", "лимит времени запроса"),
        ("~100 s (Cloudflare)", "~100 с (Cloudflare)"),
        ("120 s (OpenAI)", "120 с (OpenAI)"),
    ),
    _row(("proven on this PC", "проверено на этом ПК"), ("yes", "да"), ("not yet", "ещё нет")),
)

INTRO = {
    "en": (
        "Two ways to connect ChatGPT to this PC (neither needs the ChatGPT app/window open):\n"
        "  public  - Cloudflare + OAuth. Works today. Needs your domain and a Cloudflare account.\n"
        "            The URL is on the internet (guarded by an OAuth login).\n"
        "  tunnel  - OpenAI Secure MCP Tunnel. Nothing public, no domain, one process. Needs an OpenAI\n"
        "            Platform key (Tunnels Read+Use) and a tunnel linked to your ChatGPT workspace. "
        "EXPERIMENTAL here: no tool call has\n"
        "            gone through it yet.\n"
    ),
    "ru": (
        "Два способа подключить ChatGPT к этому ПК (оба работают без открытого приложения/окна ChatGPT):\n"
        "  public  - Cloudflare + OAuth. Работает сейчас. Нужны ваш домен и аккаунт Cloudflare.\n"
        "            Адрес виден в интернете (защита: вход по паролю, OAuth).\n"
        "  tunnel  - OpenAI Secure MCP Tunnel. Ничего публичного, без домена, один процесс. Нужен ключ\n"
        "            OpenAI Platform (Tunnels Read+Use) и туннель, привязанный к вашему рабочему пространству ChatGPT.\n"
        "            ЭКСПЕРИМЕНТАЛЬНО: ни один вызов инструмента через него ещё не проходил.\n"
    ),
}

OUTRO = {
    "en": "Default: public (verified on this PC). It never changes by itself; the tunnel is opt-in:\n"
    "`init --mode tunnel --tunnel-id <id>`.\n"
    "Both stop working when this PC sleeps. ChatGPT only needs to be open while you create the connector.\n",
    "ru": "По умолчанию: public (проверено на этом ПК); сам по себе режим не меняется. Tunnel только явно:\n"
    "`init --mode tunnel --tunnel-id <id>`.\n"
    "Оба режима перестают работать, когда ПК спит. ChatGPT нужен открытым только при создании коннектора.\n",
}


def render(language: str) -> str:
    """The intro, an aligned table and the outro in `language` (`en` or `ru`; anything else falls back to English)."""
    lang = language if language in LANGUAGES else "en"
    first = max(len(row.label[lang]) for row in ROWS) + 2
    second = max(len(row.public[lang]) for row in ROWS + (_row(("", ""), ("public", "public"), ("", "")),)) + 2
    lines = [INTRO[lang], f"{'':<{first}}{'public':<{second}}tunnel"]
    lines += [f"{row.label[lang]:<{first}}{row.public[lang]:<{second}}{row.tunnel[lang]}" for row in ROWS]
    return "\n".join(lines) + "\n\n" + OUTRO[lang]


def language_from_environment(lang_value: str) -> str:
    """`ru` for a Russian locale (LANG=ru_RU.UTF-8), else `en`."""
    return "ru" if lang_value.lower().startswith("ru") else "en"
