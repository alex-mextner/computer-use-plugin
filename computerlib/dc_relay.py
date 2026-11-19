"""The stdio relay `computer mcp serve` puts between a client and Desktop Commander: no client is offered DC's feedback channel.

WHY: Desktop Commander 0.3.2 lists `give_feedback_to_desktop_commander`, whose handler awaits `xdg-open <form URL>`
(dist/tools/feedback.js `openUrlInBrowser`), and when its remote `user_surveys` flag is on it appends a "[SYSTEM INSTRUCTION]"
asking the model to run that survey to the first successful tool result of a session (dist/server.js after the tool switch,
text from dist/utils/usageTracker.js `getFeedbackPromptMessage`). A remote model calling the tool opens a browser on the host, and
where that browser stays in the foreground the call never returns, as the welcome page did (#21). DC has no flag, env var or
config key that hides the tool (dist/server.js `shouldIncludeTool` drops it only for the client named `desktop-commander-app`,
which is not ours to claim) and its survey opt-out is a `feedbackGiven` value in its config.json that the model can set back
with `set_config_value`, so the one place every client passes through, this relay, withholds both (#24).
INVARIANTS: stdout carries only DC's own messages, in DC's order, plus the local answers below; a DC line that is not the
tools/list answer or a result carrying the survey goes through byte for byte. Every client line is read as DC's Node reads it
before it is forwarded: a feedback call is answered here, and a line Python still cannot read is answered with a parse error
rather than handed to a parser that might run it. DC gets the signals this process acts on (one it was started ignoring stays
ignored and is not passed on), dies with it, and ends it the way DC ended (`childexit.exit_like`); stderr is DC's, untouched.
"""

from __future__ import annotations

import json
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

FEEDBACK_TOOL = "give_feedback_to_desktop_commander"
_FEEDBACK_TOOL_BYTES = FEEDBACK_TOOL.encode()
_SURVEY_MARKER = b"[SYSTEM INSTRUCTION]: "
_FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_PR_SET_PDEATHSIG = 1
# Python refuses longer integer literals (sys.get_int_max_str_digits, 4300 by default); V8 reads them as a double.
_INT_DIGITS = 4000
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
    """A line Python cannot read as JSON even the way Node reads it."""


def without_survey(text: str) -> str:
    return _SURVEY.sub("", text)


def _encode(message: dict[str, object]) -> bytes:
    # ASCII escapes: a lone surrogate DC's JSON.stringify wrote as `\udXXX` survives the round trip (UTF-8 cannot encode it).
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


def _int_like_v8(digits: str) -> int | float:
    return int(digits) if len(digits.lstrip("-")) <= _INT_DIGITS else float(digits)


def _load(line: bytes | bytearray) -> object:
    """`line` read the way DC's Node reads it (the MCP SDK decodes invalid UTF-8 to U+FFFD, V8 takes any integer); raises
    _Unreadable for what Python still cannot read (nesting deeper than its recursion limit)."""
    try:
        return json.loads(line.decode("utf-8", "replace"), parse_int=_int_like_v8)
    except (ValueError, RecursionError) as error:
        raise _Unreadable from error


def local_answer(line: bytes | bytearray) -> bytes | None:
    """What the client gets instead of DC seeing `line`, or None to forward it: the answer to a feedback call (nothing for one
    sent as a notification), or a parse error for a line Python cannot read, which therefore cannot be shown to be no feedback
    call. Every line is parsed, not searched for the tool's name: a JSON string may spell it with escapes."""
    if not line.strip():
        return None
    try:
        loaded = _load(line)
    except _Unreadable:
        error: dict[str, object] = {"code": -32700, "message": "Parse error: the request cannot be read"}
        return _encode({"jsonrpc": "2.0", "id": None, "error": error})
    message = as_dict(loaded)
    if message.get("method") != "tools/call" or as_dict(message.get("params")).get("name") != FEEDBACK_TOOL:
        return None
    if "id" not in message:
        return b""
    result: dict[str, object] = {"content": [{"type": "text", "text": WITHHELD_ANSWER}], "isError": True}
    return _encode({"jsonrpc": "2.0", "id": message["id"], "result": result})


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


def filtered_server_line(line: bytes) -> bytes:
    """`line` from DC as the client may see it: no feedback tool in a tools/list answer, no survey in a tool result. The byte
    search before parsing is safe on this side: DC writes JSON.stringify output, which never escapes these ASCII characters."""
    if _FEEDBACK_TOOL_BYTES not in line and _SURVEY_MARKER not in line:
        return line
    try:
        loaded = _load(line)
    except _Unreadable:
        return line
    if not is_object(loaded):
        return line
    message = as_dict(loaded)
    if "method" in message or not is_object(message.get("result")):
        return line
    result = as_dict(message["result"])
    replaced = _without_feedback_tool(result) or _without_survey_content(result)
    return line if replaced is None else _encode({**message, "result": replaced})


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


def _pump_client(source: int, server: int, out: _Output) -> None:
    """Client lines to DC, minus the ones answered here. Reads the raw fd: this thread is still blocked in that read when the
    relay returns, and a buffered stdin's lock held at interpreter exit aborts the process."""

    def deliver(line: bytearray) -> None:
        answer = local_answer(line)
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


def relay(node: Path, args: Sequence[str]) -> int:
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
    threading.Thread(target=_pump_client, args=(sys.stdin.fileno(), server_stdin, out), daemon=True).start()
    relaying = True
    try:
        try:
            for line in iter(child.stdout.readline, b""):
                out.write(filtered_server_line(line))
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
