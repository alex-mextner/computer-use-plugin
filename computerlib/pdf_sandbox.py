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
unreadable profile means no coverage. Of the profiles whose attachment matches Chrome, only the most specific one decides, as in
the kernel. A profile on disk covers Chrome only once the kernel lists a profile of that name as loaded (its root-only list, or
the per-profile policy directories any user can read); when neither list can be read or the kernel does not list it, the warning
says the coverage is unverified instead of staying silent. The kernel shows no more than the name to a normal user, so a profile
edited after loading is judged by its file. The Chrome lookup mirrors Desktop Commander's `getChromePath()` of the pinned version
(its own cache first, then the packaged browsers it knows); a packaged browser is trusted to start its sandbox, since the distro
ships it with an AppArmor profile, a setuid `chrome-sandbox` or snap confinement.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

# Module attributes, read at call time, so tests point them at scratch files.
RESTRICTION_FILE = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
PROFILE_DIR = Path("/etc/apparmor.d")
LOADED_PROFILES = Path("/sys/kernel/security/apparmor/profiles")  # `<name> (<mode>)` per loaded profile; readable only by root
POLICY_PROFILES = Path("/sys/kernel/security/apparmor/policy/profiles")  # a directory with a `name` file per loaded profile

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

_INCLUDE = re.compile(r'^#?include(?:\s+if\s+exists)?\s+[<"](?P<name>[^>"]+)[>"]')
_VARIABLE = re.compile(r"^@\{(?P<name>\w+)\}\s*(?P<op>\+?=)\s*(?P<values>.+)$")
_REFERENCE = re.compile(r"@\{(\w+)\}")
# A line without these is cut by blanks, commas and `}` alone, which a regex does far faster than the character walk.
_GROUPING = re.compile(r'["\\\[{(]')
_PLAIN_TOKENS = re.compile(r"[,}]|[^\s,}]+")
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


@dataclass(frozen=True)
class _Attachment:
    """One glob a top-level profile attaches to (a variable with two values gives the profile two)."""

    profile: str  # the name as written in the header
    names: frozenset[str]  # the names the kernel may list the profile under once it is loaded
    file: Path
    pattern: re.Pattern[str]
    body: tuple[str, ...]  # the profile's rule lines

    @cached_property
    def grants(self) -> bool:
        """The profile allows `userns` and does not deny it. Read only for the attachments that matter to Chrome: following
        every profile's includes would cost the scan most of its time."""
        # Profiles are the files directly in the profile directory, the base of their relative includes.
        allowed, denied = _userns_rules(list(self.body), self.file.parent)
        return allowed and not denied


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


def _span_end(line: str, index: int) -> int:
    """One past the escape, quoted string or `[...]` class that starts at `index`; index + 1 for any other character."""
    char = line[index]
    if char == "\\":
        return index + 2
    if char == '"':
        end = index + 1
        while end < len(line) and line[end] != '"':
            end += 2 if line[end] == "\\" else 1
        return end + 1
    if char == "[" and "]" in line[index + 1 :]:
        return line.index("]", index + 1) + 1
    return index + 1


class _Lexer:
    """One line cut the way AppArmor's lexer cuts it, as far as this check needs: words, and `,`, `{` and `}` where they are rule
    or block syntax. A word keeps its quotes, escapes, `[...]` classes and `{a,b}` or `(...)` groups whole, so a comma or a brace
    inside a path (`/srv/{a,b}`, `[{]`, `\\{`) neither ends a rule nor opens a block. A block brace stands alone between
    blanks, or follows a closed `(...)` list (`flags=(complain){`)."""

    def __init__(self, line: str) -> None:
        self.line = line
        self.tokens: list[str] = []
        self.word = ""
        self.groups = 0  # `{` alternations and `(` lists open in the current word

    def tokens_of_line(self) -> list[str]:
        index = 0
        while index < len(self.line):
            index = self._step(index)
        return [*self.tokens, self.word] if self.word else self.tokens

    def _separator(self, index: int) -> str | None:
        """The syntax token the character at `index` stands for ("" for a blank), or None when it belongs to a word."""
        char = self.line[index]
        if self.groups:
            return None
        if char == "{":
            standalone = self.word[-1:] in ("", ")") and self.line[index + 1 : index + 2] in ("", " ", "\t")
            return "{" if standalone else None
        if char in "},":
            return char
        return "" if char in " \t" else None

    def _step(self, index: int) -> int:
        separator = self._separator(index)
        if separator is not None:
            self.tokens += [token for token in (self.word, separator) if token]
            self.word = ""
            return index + 1
        char = self.line[index]
        if char in "{(":
            self.groups += 1
        elif char in "})" and self.groups:
            self.groups -= 1
        end = _span_end(self.line, index)
        self.word += self.line[index:end]
        return end


def _tokens(line: str) -> list[str]:
    if _GROUPING.search(line) is None:
        return _PLAIN_TOKENS.findall(line)
    return _Lexer(line).tokens_of_line()


def _unquoted(word: str) -> str:
    """A word without its quote marks; escapes stay for the glob reader."""
    return re.sub(r'(\\.)|"', lambda match: match[1] or "", word)


def _is_path(word: str) -> bool:
    return word.startswith(("/", "@{"))


def _header(tokens: list[str]) -> tuple[str, str | None] | None:
    """(name, attachment) of a profile header (`profile NAME [ATTACHMENT] [flags] {` or `/path [flags] {`); the attachment is
    None for a profile that attaches to nothing."""
    if tokens[-1:] != ["{"]:
        return None
    words = [_unquoted(word) for word in tokens[:-1]]
    if words[:1] == ["profile"] and len(words) > 1:
        # Without an attachment, a profile named by a path attaches to that path.
        attachment = words[2] if len(words) > 2 and _is_path(words[2]) else words[1]
        return words[1], attachment if _is_path(attachment) else None
    if words and _is_path(words[0]):
        return words[0], words[0]
    return None


def _depth_change(tokens: list[str]) -> int:
    return tokens.count("{") - tokens.count("}")


def _kernel_name(name: str, variables: dict[str, list[str]], depth: int = 0) -> str | None:
    """The name the kernel lists a profile named with variables under: apparmor_parser writes a variable with several values as a
    `{a,b}` alternation and, unlike in an attachment, keeps doubled slashes. None for an undefined variable or a cycle."""
    reference = _REFERENCE.search(name)
    if reference is None:
        return name
    values = variables.get(reference[1], [])
    if not values or depth > 10:
        return None
    value = values[0] if len(values) == 1 else "{" + ",".join(values) + "}"
    return _kernel_name(name[: reference.start()] + value + name[reference.end() :], variables, depth + 1)


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


_GLOB_SINGLE = {"*": "([^/]*)", "?": "[^/]"}


def _glob_piece(glob: str, index: int, depth: int) -> tuple[str, int, int]:
    """The regex for the glob syntax at `index`, the index after it, and the `{` alternation depth after it."""
    char = glob[index]
    if char == "\\" and index + 1 < len(glob):
        return re.escape(glob[index + 1]), index + 2, depth
    if glob.startswith("**", index):
        return "(.*)", index + 2, depth
    if char in _GLOB_SINGLE:
        return _GLOB_SINGLE[char], index + 1, depth
    if char == "[" and "]" in glob[index + 1 :]:
        end = glob.index("]", index + 1) + 1
        return glob[index:end], end, depth
    if char == "{":
        return "(?:", index + 1, depth + 1
    if depth and char in "},":
        return (")", index + 1, depth - 1) if char == "}" else ("|", index + 1, depth)
    return re.escape(char), index + 1, depth


def _glob_regex(glob: str) -> re.Pattern[str]:
    """AppArmor's globbing as a regex: `**` crosses `/`, `*` and `?` do not, `{a,b}` alternates (nested), `[...]` is a character
    class, a backslash makes the next character literal. Every `*` and `**` is a capture group, so a match tells how much of the
    path came before the first one."""
    out: list[str] = []
    depth = 0
    index = 0
    while index < len(glob):
        piece, index, depth = _glob_piece(glob, index, depth)
        out.append(piece)
    try:
        return re.compile("".join(out))
    except re.error:
        return re.compile(r"(?!)")  # a pattern this reader cannot follow covers nothing


def _included(profiles: Path, name: str) -> list[Path]:
    """The files `include <name>` (relative to the profile directory) or `include "/path"` names; a directory means its files.
    A directory this user cannot list gives nothing: its rules stay unknown, so they never count as a grant."""
    target = Path(name) if name.startswith("/") else profiles / name
    if target.is_dir():
        try:
            return sorted(path for path in target.iterdir() if _loaded(path))
        except OSError:
            return []
    return [target] if target.is_file() else []


def _rule_userns(words: list[str]) -> tuple[bool, bool]:
    """(allowed, denied) by one rule, given as its words without the closing comma."""
    rest = words[1:] if words[:1] == ["audit"] else words
    if rest[:1] == ["deny"]:
        return False, rest[1:2] == ["userns"]
    rest = rest[1:] if rest[:1] == ["allow"] else rest
    return rest in (["userns"], ["userns", "create"]), False


def _line_userns(tokens: list[str]) -> tuple[bool, bool]:
    """(allowed, denied) by one line. Rules end at a comma and a line may hold several (`userns, deny userns,`): each counts."""
    allowed = denied = False
    rule: list[str] = []
    for token in tokens:
        if token != ",":
            rule.append(token)
            continue
        more_allowed, more_denied = _rule_userns(rule)
        allowed = allowed or more_allowed
        denied = denied or more_denied
        rule = []
    # A grant needs its comma; a deny counts without one, so a typo never turns into a grant.
    return allowed, denied or _rule_userns(rule)[1]


def _userns_rules(body: list[str], profiles: Path, nesting: int = 0) -> tuple[bool, bool]:
    """(allowed, denied) for `userns` by the rules of one profile body, its includes followed; child profiles do not count."""
    allowed = denied = False
    depth = 0
    for rule in body:
        tokens = _tokens(rule)
        include = _INCLUDE.match(rule)
        if depth == 0 and include is not None and nesting < _INCLUDE_DEPTH:
            for path in _included(profiles, include["name"]):
                more_allowed, more_denied = _userns_rules(_lines(path), profiles, nesting + 1)
                allowed, denied = allowed or more_allowed, denied or more_denied
        elif depth == 0:
            more_allowed, more_denied = _line_userns(tokens)
            allowed, denied = allowed or more_allowed, denied or more_denied
        depth += _depth_change(tokens)
    return allowed, denied


def _top_level_profiles(lines: list[str]) -> list[tuple[str, str | None, list[str]]]:
    """(name, attachment, body lines) of the top-level profiles in one file. A child profile (a header inside another profile's
    braces) is not attached globally, so it is skipped."""
    tokenized = [_tokens(line) for line in lines]
    found: list[tuple[str, str | None, list[str]]] = []
    file_depth = 0
    for start, tokens in enumerate(tokenized):
        header = _header(tokens) if file_depth == 0 else None
        # Clamped, so one brace this reader gets wrong cannot hide every later profile in the file.
        file_depth = max(0, file_depth + _depth_change(tokens))
        if header is None:
            continue
        depth = 1
        body: list[str] = []
        for rule, rule_tokens in zip(lines[start + 1 :], tokenized[start + 1 :], strict=True):
            depth += _depth_change(rule_tokens)
            if depth <= 0:
                break
            body.append(rule)
        found.append((*header, body))
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


def _file_attachments(path: Path, tunables: dict[str, list[str]]) -> list[_Attachment]:
    """Every glob a top-level profile of one profile file attaches to."""
    lines = _lines(path)
    variables = _variables(lines, {name: list(values) for name, values in tunables.items()})
    found: list[_Attachment] = []
    for name, attachment, body in _top_level_profiles(lines):
        names = frozenset(listed for listed in (name, _kernel_name(name, variables)) if listed is not None)
        globs = _expand(attachment, variables) if attachment is not None else []
        found += [_Attachment(name, names, path, _glob_regex(glob), tuple(body)) for glob in globs]
    return found


def _attachments(profiles: Path) -> list[_Attachment]:
    """Every glob a top-level profile of the enabled files in `profiles` attaches to."""
    tunables = _tunables(profiles)
    return [attachment for path in _enabled_profiles(profiles) for attachment in _file_attachments(path, tunables)]


def _most_specific(attachments: list[_Attachment], path: str) -> list[_Attachment]:
    """One attachment per profile AppArmor would pick for `path`: one profile, or several when they tie, and then the kernel
    attaches none of them ("conflicting profile attachments").

    The kernel's rule (security/apparmor/domain.c, find_attach): the attachment that matched more of the path before its first
    `*` or `**` wins. A plain path matches all of it, so it wins over any glob with a star; for the usual pattern the rule is the
    longest literal prefix. Copies of one profile name are one profile to the kernel, so they never tie with each other."""
    best = -1
    top: dict[str, _Attachment] = {}
    for attachment in attachments:
        match = attachment.pattern.fullmatch(path)
        if match is None:
            continue
        stars = [match.start(group) for group in range(1, attachment.pattern.groups + 1) if match.start(group) != -1]
        matched = min(stars, default=len(path))
        if matched > best:
            best, top = matched, {}
        if matched == best:
            top.setdefault(attachment.profile, attachment)
    return list(top.values())


def _ambiguous(attachments: list[_Attachment], path: str) -> dict[str, list[Path]]:
    """The profile names defined in several files whose copies do not all attach to `path` and grant `userns`, with those files.
    The kernel holds one copy per name (from the file loaded last), and which one that is cannot be told from here."""
    copies: dict[str, dict[Path, list[_Attachment]]] = {}
    for attachment in attachments:
        copies.setdefault(attachment.profile, {}).setdefault(attachment.file, []).append(attachment)
    return {
        name: sorted(files)
        for name, files in copies.items()
        if len(files) > 1
        and not all(any(glob.pattern.fullmatch(path) is not None for glob in copy) and copy[0].grants for copy in files.values())
    }


def _covering(winners: list[_Attachment], ambiguous: dict[str, list[Path]]) -> bool:
    return len(winners) == 1 and winners[0].grants and winners[0].profile not in ambiguous


def _policy_names() -> frozenset[str] | None:
    """The loaded profile names from the per-profile policy directories any user may read; None when they cannot be read."""
    try:
        entries = list(POLICY_PROFILES.iterdir())
    except OSError:
        return None
    names: set[str] = set()
    for entry in entries:
        try:
            names.add((entry / "name").read_text(errors="replace").strip())
        except FileNotFoundError:
            continue  # replaced or removed while this ran
        except OSError:
            return None  # an unreadable entry leaves the set unknown, not empty
    return frozenset(names)


def _loaded_profiles() -> frozenset[str] | None:
    """The names of the profiles the kernel has loaded, or None when this user can read neither of its lists."""
    names = _policy_names()
    if names is not None:
        return names
    try:
        listing = LOADED_PROFILES.read_text(errors="replace")
    except OSError:
        return None
    # One `<name> (<mode>)` per line.
    return frozenset(line.rpartition(" (")[0] for line in listing.splitlines() if " (" in line)


def _uncovered(deciding: list[_Attachment], ambiguous: dict[str, list[Path]], kernel_known: bool) -> str:
    """Why the profiles that decide for Chrome do not let it create a user namespace."""
    if len(deciding) > 1:
        names = ", ".join(f"{attachment.profile} ({attachment.file})" for attachment in deciding)
        return f"the profiles {names} attach to it equally specifically, so AppArmor applies none of them"
    if deciding and deciding[0].profile in ambiguous:
        files = ", ".join(str(file) for file in ambiguous[deciding[0].profile])
        return (
            f"profile {deciding[0].profile}, the most specific one attaching to it, is defined differently in {files}, and the "
            "kernel holds only one of them"
        )
    if deciding:
        return (
            f"profile {deciding[0].profile} in {deciding[0].file}, the most specific profile attaching to it, does not allow "
            "`userns,`"
        )
    return f"no {'loaded profile' if kernel_known else f'profile in {PROFILE_DIR}'} allows `userns,` for it"


_RESTRICTION = "AppArmor restricts unprivileged user namespaces (kernel.apparmor_restrict_unprivileged_userns=1)"
_STOPS = 'Chrome stops with "No usable sandbox!" (the shell and file tools are not affected)'


def _unverified(chrome: str, found: _Attachment, kernel_known: bool) -> Problem:
    """A profile on disk would cover Chrome, but the kernel does not confirm it has loaded it."""
    unconfirmed = (
        "the kernel's loaded profiles do not include it"
        if kernel_known
        else f"whether the kernel loaded it cannot be confirmed (neither {POLICY_PROFILES} nor {LOADED_PROFILES} is readable)"
    )
    return Problem(
        detail=(
            f"write_pdf's Chrome ({chrome}) is covered only on disk (unverified): {_RESTRICTION} and profile {found.profile} in "
            f"{found.file} allows `userns,` for it, but {unconfirmed}; unless it is loaded, {_STOPS}"
        ),
        fix=f"as root, load it with `apparmor_parser -r {shlex.quote(str(found.file))}` (`aa-status` lists loaded profiles)",
    )


def _restricted() -> bool:
    try:
        return RESTRICTION_FILE.read_text().strip() == "1"
    except OSError:
        return False


def problem(home: Path | None = None) -> Problem | None:
    """Why write_pdf's Chrome cannot start its sandbox on this machine and how to fix it, or None when nothing stops it."""
    home = home if home is not None else Path.home()
    launched = _pdf_chrome(home) if _restricted() else None
    if launched is None:
        return None
    # AppArmor matches the attachment against the executable's resolved path.
    resolved = os.path.realpath(launched)
    attachments = _attachments(PROFILE_DIR)
    ambiguous = _ambiguous(attachments, resolved)
    loaded = _loaded_profiles()
    applied = None if loaded is None else _most_specific([one for one in attachments if one.names & loaded], resolved)
    if applied is not None and _covering(applied, ambiguous):
        return None
    on_disk = _most_specific(attachments, resolved)
    chrome = str(launched) if launched.exists() else f"the Chrome for Testing it downloads into {_cache_dir(home)}"
    if _covering(on_disk, ambiguous):
        return _unverified(chrome, on_disk[0], loaded is not None)
    # With the kernel's list at hand, the loaded profiles are what Chrome meets.
    why = _uncovered(on_disk if applied is None else applied, ambiguous, applied is not None)
    glob = _cache_dir(home) / "*" / "chrome-linux64" / "chrome"
    return Problem(
        detail=f"write_pdf cannot start Chrome ({chrome}): {_RESTRICTION} and {why}, so {_STOPS}",
        fix=(
            f"as root, add an AppArmor profile that allows `userns,` for {glob} and load it with `apparmor_parser -r` (the "
            f"README has the profile; {DOCS_URL}), or lift the restriction for every program with "
            "`sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (weakens a system-wide protection)"
        ),
    )
