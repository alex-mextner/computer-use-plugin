"""Ending a wrapper process the way the child it wraps ended, shared by every wrapper (`chatgpt quiet`, the `mcp serve` relay).

A child that dies of a signal makes its wrapper die of the same signal, so a supervisor sees what it would have seen without the
wrapper (systemd counts a stop by SIGTERM as clean; returned as a status, -15 would become exit 241, a failed unit). A signal that
writes a core (SIGSEGV, SIGABRT, ...) or that cannot be re-raised (libc-reserved, 32 and 33 on glibc) becomes the shell's exit
status 128+N instead: re-raising a core signal would dump the wrapper's memory, which holds the traffic it relayed.
"""

from __future__ import annotations

import signal

CORE_SIGNALS = frozenset(
    {
        signal.SIGQUIT,
        signal.SIGILL,
        signal.SIGTRAP,
        signal.SIGABRT,
        signal.SIGBUS,
        signal.SIGFPE,
        signal.SIGSEGV,
        signal.SIGXCPU,
        signal.SIGXFSZ,
        signal.SIGSYS,
    }
)


def exit_like(code: int) -> int:
    """End this process the way the child ended (`code` is a Popen returncode): its own status, or death by the same signal.
    Returns the exit status when that signal is not re-raised (see the module docstring for which)."""
    if code >= 0:
        return code
    signum = -code
    if signum not in CORE_SIGNALS:
        try:
            if signum != signal.SIGKILL:  # SIGKILL's disposition is always the default; setting it raises OSError
                signal.signal(signum, signal.SIG_DFL)
        except OSError:  # libc-reserved: cannot be reset, so this process cannot die the same way
            pass
        else:
            signal.raise_signal(signum)  # returns only when an inherited signal mask blocks it
    return 128 + signum
