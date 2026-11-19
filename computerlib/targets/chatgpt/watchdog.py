"""A bounded watchdog pass for units that stay active but stop answering (systemd's Restart=always covers only an exit).

WHAT: `heal_pass` probes every watched unit once and counts consecutive failures in a JSON state file. A unit is restarted only
when all of these hold: it is active, it failed `threshold` passes in a row, the internet is reachable, fewer than `budget`
restarts of it happened in the last hour, and its last restart is at least `spacing` seconds ago. `exclusive` is the flock that
keeps two passes (a timer and a hand run) from overlapping. `reachable` is the connectivity gate.
WHY LOCAL: the agent-tools watchdog lib (ultrabricks/agent-tools#1205) has not shipped; `Watched`, `Policy`, `heal_pass`,
`exclusive` and `reachable` are the minimum it should replace, kept free of chatgpt specifics so the swap is one import.
INVARIANTS: only the units passed are restarted, one `restart` call per unit; a unit that is not active is left to systemd, and
`restart` must leave a unit that stopped during the pass stopped (it returns False then: a `stop` or `end` wins over heal); a
failure explained by the unit it depends on (the shim proxies the bridge, so a hung bridge makes the shim's probe hang too) is not
counted against it; no restart while the internet is unreachable, and the failure counts start again from zero after an outage
(the outage, not the unit, may have failed those probes); a restart is recorded in the budget before it runs, so a restart that
fails still spends it; warning-priority lines are written only when the pass restarts a unit or when the budget first stops a
restart of a failing streak.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from computerlib.fileio import atomic_write
from computerlib.targets.chatgpt.shim.jsonish import as_list, as_object

# syslog priorities, as the journal reads them from a `<N>` line prefix
WARNING = 4
INFO = 6
DEBUG = 7

Say = Callable[[int, str], None]


@dataclass(frozen=True)
class Watched:
    unit: str
    probe: Callable[[], str | None]  # None when healthy, else what failed
    depends_on: str = ""  # a watched unit whose failure explains this one's


@dataclass(frozen=True)
class Policy:
    threshold: int = 3  # consecutive failing passes before a restart
    budget: int = 3  # restarts of one unit per window
    spacing: float = 600.0  # seconds between two restarts of one unit
    window: float = 3600.0


DEFAULT_POLICY = Policy()


@dataclass
class _Record:
    failures: int = 0
    restarts: list[float] = field(default_factory=list[float])


def _load(path: Path) -> dict[str, _Record]:
    try:
        raw = as_object(json.loads(path.read_text()))
    except (OSError, ValueError):
        return {}  # no file yet, or a torn one: counting again from zero only delays a restart
    records: dict[str, _Record] = {}
    for unit, value in as_object(raw.get("units")).items():
        fields = as_object(value)
        failures = fields.get("failures")
        restarts = [t for t in as_list(fields.get("restarts")) if isinstance(t, int | float)]
        records[unit] = _Record(failures if isinstance(failures, int) else 0, [float(t) for t in restarts])
    return records


def _save(path: Path, records: dict[str, _Record]) -> None:
    units = {unit: {"failures": r.failures, "restarts": r.restarts} for unit, r in records.items()}
    atomic_write(path, json.dumps({"units": units}, indent=2) + "\n")


@contextmanager
def exclusive(lock_file: Path) -> Generator[bool]:
    """True while this process holds the lock; False at once when another pass holds it (never waits)."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True  # closing the file releases the lock


def reachable(anchors: Sequence[tuple[str, int]], timeout: float = 3.0) -> bool:
    """Any TCP answer from one of `anchors` (addresses, so a DNS outage alone does not read as the internet being down)."""
    for host, port in anchors:
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def on_journal(stream: TextIO) -> bool:
    """Whether `stream` is connected to the journal: systemd sets JOURNAL_STREAM to the device:inode of that stream."""
    expected = os.environ.get("JOURNAL_STREAM", "")
    if not expected:
        return False
    try:
        info = os.fstat(stream.fileno())
    except (OSError, ValueError):
        return False  # not a real file descriptor (captured output, a closed stream)
    return f"{info.st_dev}:{info.st_ino}" == expected


def _failing(watched: Sequence[Watched], active_state: Callable[[str], str], say: Say) -> tuple[set[str], dict[str, str]]:
    """(healthy units, failing unit -> reason) of the active units; a unit that is not active is neither."""
    healthy: set[str] = set()
    failing: dict[str, str] = {}
    for item in watched:
        state = active_state(item.unit)
        if state != "active":
            say(DEBUG, f"heal: {item.unit} is {state}: systemd restarts it, heal does not")
            continue
        reason = item.probe()
        if reason is None:
            healthy.add(item.unit)
        else:
            failing[item.unit] = reason
    return healthy, failing


def _excuse_dependents(watched: Sequence[Watched], healthy: set[str], failing: dict[str, str], say: Say) -> None:
    for item in watched:
        if item.unit in failing and item.depends_on and item.depends_on not in healthy:
            say(INFO, f"heal: {item.unit} fails while {item.depends_on} does not serve: counted against {item.depends_on} only")
            del failing[item.unit]


def _verdict(unit: str, record: _Record, reason: str | None, now: float, policy: Policy, say: Say) -> str:
    """Counts the pass into `record` and decides: healthy, counting, spent, spacing or restart. Says why it holds back."""
    record.restarts = [t for t in record.restarts if now - t < policy.window]
    if reason is None:
        record.failures = 0
        return "healthy"
    record.failures += 1
    if record.failures < policy.threshold:
        say(INFO, f"heal: {unit} unhealthy ({record.failures}/{policy.threshold}): {reason}")
        return "counting"
    if len(record.restarts) >= policy.budget:
        # A warning when the budget first stops a restart; the passes after it repeat the news at info, not every 2 minutes.
        say(
            WARNING if record.failures == policy.threshold else INFO,
            f"heal: {unit} unhealthy for {record.failures} passes in a row and its restart budget is spent "
            f"({policy.budget} in the last hour): not restarting it: {reason}",
        )
        return "spent"
    if record.restarts and now - record.restarts[-1] < policy.spacing:
        wait = int(policy.spacing - (now - record.restarts[-1]))
        say(INFO, f"heal: {unit} unhealthy ({record.failures} passes): next restart allowed in {wait}s: {reason}")
        return "spacing"
    return "restart"


def heal_pass(
    watched: Sequence[Watched],
    *,
    state_file: Path,
    active_state: Callable[[str], str],
    restart: Callable[[str], bool],
    online: Callable[[], bool],
    clock: Callable[[], float],
    say: Say,
    policy: Policy = DEFAULT_POLICY,
) -> bool:
    """One pass over `watched`. Returns True when a unit needs a restart its budget no longer allows (a crash loop)."""
    said: list[int] = []

    def tell(priority: int, text: str) -> None:
        said.append(priority)
        say(priority, text)

    records = _load(state_file)
    now = clock()
    healthy, failing = _failing(watched, active_state, tell)
    _excuse_dependents(watched, healthy, failing, tell)
    if failing and not online():
        tell(INFO, "heal: the internet is unreachable: no restarts while it is down (failure counts start again)")
        _save(state_file, {unit: _Record(0, r.restarts) for unit, r in records.items()})
        return False
    spent = False
    for item in watched:
        record = records.setdefault(item.unit, _Record())
        reason = failing.get(item.unit)
        verdict = _verdict(item.unit, record, reason, now, policy, tell)
        spent = spent or verdict == "spent"
        if verdict == "restart":
            record.restarts.append(now)
            record.failures = 0
            _save(state_file, records)
            if not restart(item.unit):
                tell(INFO, f"heal: {item.unit} stopped during the pass: not restarted (a stop or end wins over heal)")
                continue
            tell(
                WARNING,
                f"heal: restarted {item.unit} ({len(record.restarts)} of {policy.budget} this hour): {reason}, "
                f"{policy.threshold} passes in a row",
            )
    _save(state_file, records)
    if not said:
        tell(DEBUG, f"heal: {len(watched)} unit(s) healthy, nothing to do")
    return spent
