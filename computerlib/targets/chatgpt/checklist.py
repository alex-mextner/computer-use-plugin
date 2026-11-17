"""The live checklist of `computer chatgpt connect` (public mode), driven only by what reached this PC.

A step is ticked when the access log shows its request. ChatGPT's connector object cannot be inspected from here (no API), so the
only proof that the connection works is a tools/call from a client the shim could VERIFY as ChatGPT (see shim/clients.py): nothing
earlier counts as connected, and a call labelled `unverified` (curl, a forged User-Agent) never ticks the client steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from computerlib.targets.chatgpt.accessview import Record
from computerlib.targets.chatgpt.shim.rules import PRM

STEPS: tuple[tuple[str, str], ...] = (
    ("discovery", "ChatGPT asked for the OAuth settings (protected-resource metadata / 401 challenge)"),
    ("registration", "ChatGPT registered itself as an OAuth client (POST /.idp/register)"),
    ("login", "the login page accepted the password (POST /.auth/login)"),
    ("token", "ChatGPT exchanged the code for a token (POST /.idp/token)"),
    ("initialize", "ChatGPT opened an MCP session (initialize)"),
    ("tools_list", "ChatGPT listed the tools (tools/list)"),
    ("tools_call", "ChatGPT ran a tool (tools/call): the connection works"),
)


@dataclass(frozen=True)
class Event:
    key: str
    text: str
    warning: bool = False


@dataclass
class Checklist:
    done: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    _warned: set[str] = field(default_factory=lambda: set[str]())

    @property
    def connected(self) -> bool:
        return "tools_call" in self.done

    def feed(self, record: Record) -> list[Event]:
        events = [*self._oauth(record), *self._mcp(record)]
        return [e for e in events if self._new(e)]

    def _new(self, event: Event) -> bool:
        if event.warning:
            if event.key in self._warned:
                return False
            self._warned.add(event.key)
            return True
        if event.key in self.done:
            return False
        self.done[event.key] = event.text
        return True

    @staticmethod
    def _step(key: str) -> Event:
        return Event(key, dict(STEPS)[key])

    def _oauth(self, r: Record) -> list[Event]:
        if r.method == "GET" and r.path.startswith(PRM) or r.path == "/mcp" and r.status == 401:
            return [self._step("discovery")]
        if r.method != "POST":
            return []
        outcomes = {
            "/.idp/register": ("registration", r.status == 201, "registration was refused"),
            "/.auth/login": ("login", r.status in (302, 303), "the password was wrong or the login failed"),
            "/.idp/token": ("token", r.status == 200, "the token exchange failed"),
        }
        outcome = outcomes.get(r.path)
        if outcome is None:
            return []
        key, ok, problem = outcome
        if ok:
            return [self._step(key)]
        return [Event(f"{key}-failed", f"{problem} (HTTP {r.status})", warning=True)] if r.status >= 400 else []

    def _mcp(self, r: Record) -> list[Event]:
        if r.path != "/mcp" or not 200 <= r.status < 300 or not r.rpc:
            return []
        if not r.verified_openai:
            return [
                Event(
                    "unverified",
                    "an MCP call arrived but could not be verified as ChatGPT (client=unverified): it does not count",
                    True,
                )
            ]
        events: list[Event] = []
        if "initialize" in r.rpc or "server/discover" in r.rpc:
            events.append(self._step("initialize"))
        if "tools/list" in r.rpc:
            events.append(self._step("tools_list"))
        if "tools/call" in r.rpc:
            events.append(self._step("tools_call"))
        return events
