"""The stdio relay `computer mcp serve` puts between a client and Desktop Commander: no client is offered DC's feedback channel.

WHY: Desktop Commander 0.3.2 lists `give_feedback_to_desktop_commander`, whose handler awaits `xdg-open <form URL>`
(dist/tools/feedback.js `openUrlInBrowser`), and when its remote `user_surveys` flag is on it appends a "[SYSTEM INSTRUCTION]"
asking the model to run that survey to the first successful tool result of a session (dist/server.js after the tool switch,
text from dist/utils/usageTracker.js `getFeedbackPromptMessage`). A remote model calling the tool opens a browser on the host, and
where that browser stays in the foreground the call never returns, as the welcome page did (#21). DC has no flag, env var or
config key that hides the tool (dist/server.js `shouldIncludeTool` drops it only for the client named `desktop-commander-app`,
which is not ours to claim) and its survey opt-out is a `feedbackGiven` value in its config.json that the model can set back
with `set_config_value`, so the one place every client passes through, this relay, withholds both (#24).
ChatGPT selects the terminal-only profile (#79): only terminal/process tools are advertised, UI attachment metadata/resources
are withheld, and stale file or UI-origin calls are answered locally without running them. Generic MCP and Claude retain the
default profile.
INVARIANTS: stdout carries DC's messages in DC's order, plus the local answers below. An unchanged message passes through byte
for byte; an answer that must be cleaned but cannot be encoded is replaced by an error, never passed on uncleaned.
The terminal profile consumes resource notifications and non-message child output.
Every client line is read as DC's Node reads it before forwarding: a feedback call is answered here, an unreadable line gets
a parse error, and one nested deeper
than _MAX_DEPTH with an invalid-request error, rather than handed to a parser that might run it. DC gets the signals this
process acts on (one it was started ignoring stays ignored and is not passed on), dies with it, and ends it the way DC ended
(`childexit.exit_like`); stderr is DC's, untouched.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType
from typing import cast

from computerlib.childexit import exit_like
from computerlib.jsonvalue import as_dict, is_object

# The ChatGPT adapter is intentionally terminal-only. An allowlist also withholds future widget tools by default.
TERMINAL_TOOLS = frozenset(
    {
        "start_process",
        "read_process_output",
        "interact_with_process",
        "force_terminate",
        "list_sessions",
        "list_processes",
        "kill_process",
    }
)
TERMINAL_INSTRUCTIONS = (
    "This connection provides terminal/process tools only. Use start_process with bounded shell commands for file work; "
    "use interact_with_process and read_process_output to continue existing processes. Keep each output limited to the "
    "relevant lines or a concise summary. Direct file tools and interactive UI previews are unavailable. "
    "Do not retry unavailable file/UI calls. For command calls, use origin='llm' or omit origin. "
    "Tool schemas, safety annotations and configured command restrictions still apply."
)
_TERMINAL_UNAVAILABLE = (
    "This connection provides terminal/process tools only; direct file tools and UI-origin calls are unavailable. "
    "Do not retry this file/UI call. Use start_process with bounded shell commands instead."
)
_UI_UNAVAILABLE = (
    "UI-origin terminal calls are unavailable. Do not retry this UI request. "
    "For command work, call the terminal tool with origin='llm' or omit origin; use bounded shell commands."
)
_UI_META_KEYS = frozenset({"ui", "ui/resourceUri", "openai/outputTemplate", "openai/widgetAccessible"})
FEEDBACK_TOOL = "give_feedback_to_desktop_commander"
_FEEDBACK_TOOL_BYTES = FEEDBACK_TOOL.encode()
_SURVEY_MARKER = b"[SYSTEM INSTRUCTION]: "
_FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_PR_SET_PDEATHSIG = 1
# Python refuses longer integer literals (sys.get_int_max_str_digits, 4300 by default); V8 reads them as a double.
_INT_DIGITS = 4000
# A client line nested deeper is refused outright. How deep Python's json can go depends on the interpreter (3.13 stops near
# 10 000 levels, 3.14 went past 100 000), so without a fixed cap the same request would be inspected on one machine and refused
# on another. No MCP request nests anywhere near this. DC's own lines are not capped: refusing one there would pass it on
# uncleaned.
_MAX_DEPTH = 256
_TOO_DEEP = f"Invalid request: nested deeper than {_MAX_DEPTH} levels"
# Every survey variant of DC 0.3.2 has this shape: one instruction line, the quoted request ending in DC's own turn-off line and a
# rule, then one closing sentence, appended at the end of the text (only DC's Docker notice may follow it). Matching the whole
# shape, never across another instruction header and only where DC puts it, keeps DC's other notices and any file content that
# quotes part of the survey intact. The parts are bounded (DC's are about 110, 250 and 40 characters): an unbounded lazy loop
# grows the regex engine's stack per character, so a tool result with the header and a line of many MB would exhaust memory.
_HEADER = r"\n\n\[SYSTEM INSTRUCTION\]: "
_LINE = r"(?:(?!\[SYSTEM INSTRUCTION\])[^\n]){0,400}?"
_SPAN = r"(?:(?!\[SYSTEM INSTRUCTION\]).){0,2000}?"
_SURVEY = re.compile(
    rf"{_HEADER}{_LINE}Format it like: '\n\n---\n\n\*\*Desktop Commander Team request\*\*\n\n"
    rf"{_SPAN}\*This request disappears after you give feedback or set feedbackGiven=true\*\n\n---\n\n'\. {_LINE}\."
    rf"(?=\Z|{_HEADER})",
    re.DOTALL,
)
WITHHELD_ANSWER = (
    f"{FEEDBACK_TOOL} is not available here: it opens a browser on the computer that runs Desktop Commander, where nobody using "
    "this connection may be looking, and it can block until that browser is closed."
)


class _Unreadable(Exception):
    """A line Python cannot read as JSON even the way Node reads it (its json gave up, or it is not JSON)."""


def without_survey(text: str) -> str:
    return _SURVEY.sub("", text)


def _encode(message: dict[str, object]) -> bytes:
    # ASCII escapes: a lone surrogate DC's JSON.stringify wrote as `\udXXX` survives the round trip (UTF-8 cannot encode it).
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


def _int_like_v8(digits: str) -> int | float:
    return int(digits) if len(digits.lstrip("-")) <= _INT_DIGITS else float(digits)


def _deeper_than(value: object, limit: int) -> bool:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if not isinstance(item, (dict, list)):
            continue
        if depth > limit:
            return True
        children = cast("dict[str, object]", item).values() if isinstance(item, dict) else cast("list[object]", item)
        pending.extend((child, depth + 1) for child in children)
    return False


def _load(line: bytes | bytearray) -> object:
    """`line` read the way DC's Node reads it (the MCP SDK decodes invalid UTF-8 to U+FFFD, V8 takes any integer); raises
    _Unreadable for what Python cannot read."""
    try:
        return json.loads(line.decode("utf-8", "replace"), parse_int=_int_like_v8)
    except (ValueError, RecursionError) as error:
        raise _Unreadable from error


def _error(request_id: object, code: int, message: str) -> bytes:
    error: dict[str, object] = {"code": code, "message": message}
    return _encode({"jsonrpc": "2.0", "id": request_id, "error": error})


def _reply_id(message: dict[str, object]) -> object:
    """The id an answer echoes: JSON-RPC ids are a string, a number or null, so anything else (a nested array or object could be
    too deep to encode, ending the relay) is answered with id null. bool is excluded (an int to Python, not to JSON), and so is
    a float that is not finite (1e400 reads as inf), which json.dumps would write as the non-JSON `Infinity` or `NaN`."""
    request_id = message.get("id")
    if isinstance(request_id, float):
        return request_id if math.isfinite(request_id) else None
    if request_id is None or (isinstance(request_id, (str, int)) and not isinstance(request_id, bool)):
        return request_id
    return None


def _nests_deeper_than(line: bytes | bytearray, limit: int) -> bool:
    """Whether the brackets of `line` outside its strings nest deeper than `limit`: the cap read off the bytes, for a line too
    deep for this Python's json to parse (3.13 gives up near 10 000 levels, 3.14 does not), so both refuse it the same way."""
    depth = 0
    in_string = escaped = False
    for byte in line:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > limit:
                return True
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1
    return False


def local_answer(line: bytes | bytearray, *, terminal_only: bool = False) -> bytes | None:
    """What the client gets instead of DC seeing `line`, or None to forward it: the answer to a feedback call (nothing for one
    sent as a notification), terminal-only profile calls, or an error for a line that cannot be safely forwarded: one Python
    cannot read, or one nested deeper than _MAX_DEPTH. Every line is parsed, not searched for the tool's name: JSON strings may
    spell it with escapes."""
    if not line.strip():
        return None
    # Only a line with more brackets than the cap can nest past it; the walks are skipped for every other line.
    may_nest_too_deep = line.count(b"[") + line.count(b"{") > _MAX_DEPTH
    try:
        loaded = _load(line)
    except _Unreadable:
        if may_nest_too_deep and _nests_deeper_than(line, _MAX_DEPTH):
            return _error(None, -32600, _TOO_DEEP)
        return _error(None, -32700, "Parse error: the request cannot be read")
    message = as_dict(loaded)
    if may_nest_too_deep and _deeper_than(loaded, _MAX_DEPTH):
        return _error(_reply_id(message), -32600, _TOO_DEEP)
    if terminal_only:
        if not is_object(loaded):
            return _error(None, -32600, "Invalid request: the terminal-only profile requires a JSON object")
        answer = _terminal_answer(message)
        if answer is not None:
            return answer
    if message.get("method") != "tools/call" or as_dict(message.get("params")).get("name") != FEEDBACK_TOOL:
        return None
    if "id" not in message:
        return b""
    result: dict[str, object] = {"content": [{"type": "text", "text": WITHHELD_ANSWER}], "isError": True}
    return _encode({"jsonrpc": "2.0", "id": _reply_id(message), "result": result})


def _terminal_answer(message: dict[str, object]) -> bytes | None:
    """Old cached widgets are refused here, even before a client refreshes its tool catalog."""
    method = message.get("method")
    if isinstance(method, str) and method.startswith("resources/"):
        if "id" not in message:
            return b""
        if method in ("resources/list", "resources/templates/list"):
            key = "resources" if method == "resources/list" else "resourceTemplates"
            return _encode({"jsonrpc": "2.0", "id": _reply_id(message), "result": {key: []}})
        return _error(_reply_id(message), -32601, "Resources are unavailable in the terminal-only profile")
    if method != "tools/call":
        return None
    params = as_dict(message.get("params"))
    name = params.get("name")
    if isinstance(name, str) and name in TERMINAL_TOOLS and as_dict(params.get("arguments")).get("origin") != "ui":
        return None
    if "id" not in message:
        return b""
    text = _UI_UNAVAILABLE if isinstance(name, str) and name in TERMINAL_TOOLS else _TERMINAL_UNAVAILABLE
    result: dict[str, object] = {"content": [{"type": "text", "text": text}], "isError": True}
    return _encode({"jsonrpc": "2.0", "id": _reply_id(message), "result": result})


def _without_ui_meta(value: dict[str, object]) -> dict[str, object]:
    meta = as_dict(value.get("_meta"))
    if not any(key in meta for key in _UI_META_KEYS):
        return value
    kept = {key: item for key, item in meta.items() if key not in _UI_META_KEYS}
    result = {key: item for key, item in value.items() if key != "_meta"}
    if kept:
        result["_meta"] = kept
    return result


def _terminal_tools(tools: list[object]) -> list[object]:
    kept: list[object] = []
    for item in tools:
        tool = as_dict(item)
        name = tool.get("name")
        if isinstance(name, str) and name in TERMINAL_TOOLS:
            cleaned = _without_ui_meta(tool)
            if name == "start_process":
                # The pinned public proxy drops initialize.instructions but forwards tool descriptions.
                original = cleaned.get("description")
                description = f"{original}\n\n{TERMINAL_INSTRUCTIONS}" if isinstance(original, str) else TERMINAL_INSTRUCTIONS
                cleaned = {**cleaned, "description": description}
            kept.append(cleaned)
    return kept


def _terminal_result(result: dict[str, object]) -> dict[str, object]:
    """Keep original command contracts; remove the preview attachment points, not safety annotations."""
    replaced = _without_ui_meta(result)
    tools = replaced.get("tools")
    if isinstance(tools, list):
        kept = _terminal_tools(cast("list[object]", tools))
        if kept != tools:
            replaced = {**replaced, "tools": kept}
    if "serverInfo" in replaced and is_object(replaced.get("capabilities")):
        capabilities = {key: item for key, item in as_dict(replaced["capabilities"]).items() if key != "resources"}
        replaced = {**replaced, "capabilities": capabilities, "instructions": TERMINAL_INSTRUCTIONS}
    return replaced


def _without_feedback_tool(result: dict[str, object]) -> dict[str, object] | None:
    tools = result.get("tools")
    if not isinstance(tools, list):
        return None
    listed = cast("list[object]", tools)
    kept = [tool for tool in listed if as_dict(tool).get("name") != FEEDBACK_TOOL]
    return {**result, "tools": kept} if len(kept) != len(listed) else None


def _without_survey_content(result: dict[str, object]) -> dict[str, object] | None:
    content = result.get("content")
    if not isinstance(content, list):
        return None
    kept: list[object] = []
    changed = False
    for item in cast("list[object]", content):
        entry = as_dict(item)
        text = entry.get("text")
        if entry.get("type") == "text" and isinstance(text, str):
            stripped = without_survey(text)
            if stripped != text:
                changed = True
                # DC adds the survey as an item of its own when the result does not start with text; that item is dropped whole.
                if stripped:
                    kept.append({**entry, "text": stripped})
                continue
        kept.append(item)
    return {**result, "content": kept} if changed else None


def _filtered_reply(message: dict[str, object], result: dict[str, object]) -> bytes:
    try:
        return _encode({**message, "result": result})
    except RecursionError:
        # Too deep for this interpreter's json to write back: the client gets an error for that answer, never the survey or
        # the tool.
        return _error(_reply_id(message), -32603, "Internal error: the answer is nested too deep to pass on")


def filtered_server_line(line: bytes, *, terminal_only: bool = False) -> bytes:
    """Filter DC's feedback and, when selected, file-widget exposure. The default profile's byte search is safe: DC writes
    JSON.stringify output, which never escapes these ASCII characters. The terminal profile inspects every message."""
    if not terminal_only and _FEEDBACK_TOOL_BYTES not in line and _SURVEY_MARKER not in line:
        return line
    try:
        loaded = _load(line)
    except _Unreadable:
        return b"" if terminal_only else line
    if not is_object(loaded):
        return b"" if terminal_only else line
    message = as_dict(loaded)
    method = message.get("method")
    if terminal_only and isinstance(method, str) and method.startswith("notifications/resources/"):
        return b""
    if "method" in message or not is_object(message.get("result")):
        return line
    result = as_dict(message["result"])
    replaced = _terminal_result(result) if terminal_only else result
    replaced = _without_feedback_tool(replaced) or _without_survey_content(replaced) or replaced
    if replaced is result:
        return line
    return _filtered_reply(message, replaced)


def _write_all(fd: int, data: bytes | bytearray) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


class _Output:
    """The client's stdout, shared by both directions (a withheld call is answered from the client side). Raw fd writes: a
    buffered stream's lock held by the other thread at exit makes the interpreter abort."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            _write_all(self._fd, data)


def _pump_client(source: int, server: int, out: _Output, terminal_only: bool) -> None:
    """Client lines to DC, minus the ones answered here. Reads the raw fd: this thread is still blocked in that read when the
    relay returns, and a buffered stdin's lock held at interpreter exit aborts the process."""

    def deliver(line: bytearray) -> None:
        answer = local_answer(line, terminal_only=terminal_only)
        if answer is None:
            _write_all(server, line)
        else:
            out.write(answer)

    pending = bytearray()
    try:
        while chunk := os.read(source, 65536):
            # Only the new bytes are searched: a request of many MiB arrives in many reads.
            searched = len(pending)
            pending += chunk
            while (end := pending.find(b"\n", searched)) != -1:
                deliver(pending[: end + 1])
                del pending[: end + 1]
                searched = 0
        if pending:
            deliver(pending)
    except OSError:
        pass  # a pipe closed: DC's (the server side sees its EOF and ends the relay) or the client's (it is gone)
    finally:
        os.close(server)


def _dying_with_this_process() -> Callable[[], None] | None:
    """A preexec_fn that makes the kernel SIGTERM DC when this process dies, even of SIGKILL, as DC died when it ran in this
    process's place. Linux only (the platform this tool supports)."""
    if sys.platform != "linux":
        return None
    import ctypes  # lazily: every other command would load it for nothing

    prctl = ctypes.CDLL(None, use_errno=True).prctl
    parent = os.getpid()

    def die_with_parent() -> None:
        prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        if os.getppid() != parent:  # the parent died before prctl took effect
            os.kill(os.getpid(), signal.SIGTERM)

    return die_with_parent


class _Stop(BaseException):
    """Raised by the signal handler to leave the server-side loop, which may be blocked writing to a client that stopped reading
    (the interrupted write would otherwise be retried and never return)."""


def relay(node: Path, args: Sequence[str], *, terminal_only: bool = False) -> int:
    """Run `node args` as DC with the relay in front of it on this process's stdio; returns DC's exit status."""
    to_server, server_stdin = os.pipe()
    try:
        # preexec_fn's thread hazard does not apply: the client thread starts only after this.
        child = subprocess.Popen(
            [str(node), *args],
            stdin=to_server,
            stdout=subprocess.PIPE,
            preexec_fn=_dying_with_this_process(),  # noqa: PLW1509
        )
    except BaseException:
        os.close(server_stdin)
        raise
    finally:
        os.close(to_server)
    assert child.stdout is not None
    relaying = False

    def forward(signum: int, _frame: FrameType | None) -> None:
        nonlocal relaying
        child.send_signal(signum)
        if relaying:
            relaying = False  # raised once at most, so the one `except _Stop` below catches every _Stop
            raise _Stop

    for signum in _FORWARDED:
        # An ignored signal stays ignored: whoever started serve asked for that (nohup), and stopping the relay for it would
        # cut the client off from a DC that the signal never reached.
        if signal.getsignal(signum) is not signal.SIG_IGN:
            signal.signal(signum, forward)
    out = _Output(sys.stdout.fileno())
    # A daemon: a client that keeps stdin open after DC exits must not keep this process alive.
    threading.Thread(target=_pump_client, args=(sys.stdin.fileno(), server_stdin, out, terminal_only), daemon=True).start()
    relaying = True
    try:
        try:
            for line in iter(child.stdout.readline, b""):
                out.write(filtered_server_line(line, terminal_only=terminal_only))
        except OSError:
            child.terminate()  # the client closed our stdout: nobody is left to answer
        relaying = False
    except _Stop:
        pass  # DC got the signal; what it still writes has nobody to go to
    except BaseException:
        child.terminate()
        raise
    finally:
        child.stdout.close()  # a DC still writing gets EPIPE instead of blocking on a pipe nobody reads
    return exit_like(child.wait())
