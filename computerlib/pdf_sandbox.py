"""Whether the Chrome that Desktop Commander's write_pdf launches can start its sandbox under AppArmor's userns restriction.

HOW IT IS REACHED: `desktop_commander.install()` appends `problem()` to its report as a warning (so `computer mcp install` and
every adapter's install show it); `computer chatgpt doctor` and `computer claude status` list it as a warning that does not fail
them, because the shell and file tools work either way.
WHY: Ubuntu 23.10+ sets `kernel.apparmor_restrict_unprivileged_userns=1`: a program that no AppArmor profile grants `userns,`
cannot create the user namespace Chrome's sandbox needs. The Chrome for Testing build that Desktop Commander downloads into its
own cache has no profile and no setuid `chrome-sandbox`, so write_pdf fails with "No usable sandbox!" (#14). Lifting the
restriction needs root, so this module only detects it and names the fixes from
https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md.
INVARIANTS: read-only, never writes a profile or a sysctl; an unreadable restriction file means no warning (nothing to go on), an
unreadable profile means no coverage. The Chrome lookup mirrors Desktop Commander's `getChromePath()` of the pinned version (its
own cache first, then the packaged browsers it knows); a packaged browser is trusted to start its sandbox, since the distro ships
it with an AppArmor profile, a setuid `chrome-sandbox` or snap confinement.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Module attributes, read at call time, so tests point them at scratch files.
RESTRICTION_FILE = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
PROFILE_DIR = Path("/etc/apparmor.d")

DOCS_URL = "https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md"

# Desktop Commander 0.3.2 `findSystemChrome()` on Linux, in its order.
SYSTEM_CHROMES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
)

# Stands in for the build Desktop Commander has not downloaded yet: a profile written for `<cache>/*/chrome-linux64/chrome`
# matches it as it will match the real build.
_NOT_YET_DOWNLOADED = "linux-0"

_HEADER = re.compile(r'^(?:profile\s+\S+\s+)?(?P<path>"[^"]+"|[/@]\S*)\s+(?:flags\s*=\s*)?(?:\([^)]*\)\s*)?\{\s*$')
_USERNS = re.compile(r"^(?:audit\s+)?(?:allow\s+)?userns(?:\s+create)?\s*,")
_DENY_USERNS = re.compile(r"^(?:audit\s+)?deny\s+userns\b")
_INCLUDE = re.compile(r'^#?include(?:\s+if\s+exists)?\s+[<"](?P<name>[^>"]+)[>"]')
_VARIABLE = re.compile(r"^@\{(?P<name>\w+)\}\s*(?P<op>\+?=)\s*(?P<values>.+)$")
_REFERENCE = re.compile(r"@\{(\w+)\}")
# The file names apparmor_parser skips when it loads a directory: package-manager leftovers and editor backups.
_SKIPPED_SUFFIXES = (
    "~",
    ".dpkg-new",
    ".dpkg-old",
    ".dpkg-dist",
    ".dpkg-bak",
    ".dpkg-remove",
    ".pacsave",
    ".pacnew",
    ".rpmnew",
    ".rpmsave",
    ".orig",
    ".rej",
)
_INCLUDE_DEPTH = 8


@dataclass(frozen=True)
class Problem:
    detail: str
    fix: str


def _cache_dir(home: Path) -> Path:
    return home / ".claude-server-commander" / "puppeteer-cache" / "chrome"


def _build_key(name: str) -> list[tuple[int, int, str]]:
    """Desktop Commander orders builds with `localeCompare(..., {numeric: true})`: runs of digits compare as numbers."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.lower()) for part in re.findall(r"\d+|\D+", name)]


def _pdf_chrome(home: Path) -> Path | None:
    """The Chrome write_pdf launches when it needs a profile; None when it launches a packaged browser."""
    # The newest cached build with an executable is the one Desktop Commander uses (it prunes the others).
    cached = list(_cache_dir(home).glob("*/chrome-linux64/chrome"))
    if cached:
        return max(cached, key=lambda chrome: _build_key(chrome.parents[1].name))
    if any(os.path.exists(path) for path in SYSTEM_CHROMES):
        return None
    return _cache_dir(home) / _NOT_YET_DOWNLOADED / "chrome-linux64" / "chrome"


def _loaded(path: Path) -> bool:
    return path.is_file() and not path.name.startswith(".") and not path.name.endswith(_SKIPPED_SUFFIXES)


def _lines(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    # A comment starts at `#` at the start of a line or after a blank, except the old `#include` syntax.
    return [re.sub(r"(^|\s)#(?!include\b).*$", "", line).strip() for line in text.splitlines()]


def _variables(lines: list[str], into: dict[str, list[str]]) -> dict[str, list[str]]:
    for line in lines:
        match = _VARIABLE.match(line)
        if match is None:
            continue
        values = [value.strip('"') for value in match["values"].split()]
        into[match["name"]] = [*into.get(match["name"], []), *values] if match["op"] == "+=" else values
    return into


def _expand(pattern: str, variables: dict[str, list[str]], depth: int = 0) -> list[str]:
    """One glob per combination of variable values. Variables nest (@{HOME} is @{HOMEDIRS}/*/); the depth limit stops a cycle."""
    reference = _REFERENCE.search(pattern)
    if reference is None or depth > 10:
        # @{HOME} ends in a slash and the path goes on with one; AppArmor collapses the doubled slash.
        return [re.sub(r"/+", "/", pattern)] if reference is None else []
    head, tail = pattern[: reference.start()], pattern[reference.end() :]
    # An undefined variable leaves nothing to match.
    return [glob for value in variables.get(reference[1], []) for glob in _expand(head + value + tail, variables, depth + 1)]


def _glob_regex(glob: str) -> re.Pattern[str]:
    """AppArmor's globbing: `**` crosses `/`, `*` and `?` do not, `{a,b}` alternates (nested), `[...]` is a character class."""
    out: list[str] = []
    depth = 0
    index = 0
    while index < len(glob):
        char = glob[index]
        if glob.startswith("**", index):
            out.append(".*")
            index += 1
        elif char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and "]" in glob[index + 1 :]:
            end = glob.index("]", index + 1)
            out.append(glob[index : end + 1])
            index = end
        elif char == "{":
            depth += 1
            out.append("(?:")
        elif char == "}" and depth:
            depth -= 1
            out.append(")")
        elif char == "," and depth:
            out.append("|")
        else:
            out.append(re.escape(char))
        index += 1
    try:
        return re.compile("".join(out))
    except re.error:
        return re.compile(r"(?!)")  # a pattern this reader cannot follow covers nothing


def _included(profiles: Path, name: str) -> list[Path]:
    """The files `include <name>` (relative to the profile directory) or `include "/path"` names; a directory means its files."""
    target = Path(name) if name.startswith("/") else profiles / name
    if target.is_dir():
        return sorted(path for path in target.iterdir() if _loaded(path))
    return [target] if target.is_file() else []


def _userns_rules(body: list[str], profiles: Path, nesting: int = 0) -> tuple[bool, bool]:
    """(allowed, denied) for `userns` by the rules of one profile body, its includes followed; child profiles do not count."""
    allowed = denied = False
    depth = 0
    for rule in body:
        include = _INCLUDE.match(rule)
        if depth == 0 and include is not None and nesting < _INCLUDE_DEPTH:
            for path in _included(profiles, include["name"]):
                more_allowed, more_denied = _userns_rules(_lines(path), profiles, nesting + 1)
                allowed, denied = allowed or more_allowed, denied or more_denied
        elif depth == 0:
            allowed = allowed or _USERNS.match(rule) is not None
            denied = denied or _DENY_USERNS.match(rule) is not None
        depth += rule.count("{") - rule.count("}")
    return allowed, denied


def _userns_attachments(lines: list[str], profiles: Path) -> list[str]:
    """The attachment paths of the profiles in one file that allow `userns` and do not deny it (a deny rule wins)."""
    found: list[str] = []
    for start, line in enumerate(lines):
        header = _HEADER.match(line)
        if header is None:
            continue
        depth = 1
        body: list[str] = []
        for rule in lines[start + 1 :]:
            depth += rule.count("{") - rule.count("}")
            if depth <= 0:
                break
            body.append(rule)
        allowed, denied = _userns_rules(body, profiles)
        if allowed and not denied:
            found.append(header["path"].strip('"'))
    return found


def _tunables(profiles: Path) -> dict[str, list[str]]:
    """The variables every profile sees (`include <tunables/global>` pulls them all in)."""
    found: dict[str, list[str]] = {}
    for tunable in sorted(path for path in (profiles / "tunables").rglob("*") if _loaded(path)):
        _variables(_lines(tunable), found)
    return found


def _enabled_profiles(profiles: Path) -> list[Path]:
    try:
        files = sorted(path for path in profiles.iterdir() if _loaded(path))
    except OSError:
        return []
    return [path for path in files if not (profiles / "disable" / path.name).exists()]


def _userns_patterns(profiles: Path) -> list[re.Pattern[str]]:
    """The executables the enabled profiles in `profiles` allow `userns` for, as path patterns."""
    tunables = _tunables(profiles)
    patterns: list[re.Pattern[str]] = []
    for path in _enabled_profiles(profiles):
        lines = _lines(path)
        variables = _variables(lines, {name: list(values) for name, values in tunables.items()})
        attachments = _userns_attachments(lines, profiles)
        patterns += [_glob_regex(glob) for attachment in attachments for glob in _expand(attachment, variables)]
    return patterns


def problem(home: Path | None = None) -> Problem | None:
    """Why write_pdf's Chrome cannot start its sandbox on this machine and how to fix it, or None when nothing stops it."""
    try:
        restricted = RESTRICTION_FILE.read_text().strip() == "1"
    except OSError:
        return None
    if not restricted:
        return None
    home = home if home is not None else Path.home()
    launched = _pdf_chrome(home)
    # AppArmor matches the attachment against the executable's resolved path.
    if launched is None or any(pattern.fullmatch(os.path.realpath(launched)) for pattern in _userns_patterns(PROFILE_DIR)):
        return None
    glob = _cache_dir(home) / "*" / "chrome-linux64" / "chrome"
    chrome = str(launched) if launched.exists() else f"the Chrome for Testing it downloads into {_cache_dir(home)}"
    return Problem(
        detail=(
            f"write_pdf cannot start Chrome ({chrome}): AppArmor restricts unprivileged user namespaces "
            f"(kernel.apparmor_restrict_unprivileged_userns=1) and no profile in {PROFILE_DIR} allows `userns,` for it, so "
            'Chrome stops with "No usable sandbox!" (the shell and file tools are not affected)'
        ),
        fix=(
            f"as root, add an AppArmor profile that allows `userns,` for {glob} and load it with `apparmor_parser -r` (the "
            f"README has the profile; {DOCS_URL}), or lift the restriction for every program with "
            "`sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (weakens a system-wide protection)"
        ),
    )
