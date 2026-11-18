"""Delete a directory this target owns, and prove it is gone before anyone prints "revoked"/"deleted".

WHAT: `remove_tree(path)` refuses unsafe targets, removes the tree, and raises when anything is left.
INVARIANTS: a symlink is never followed (deleting what it points at would destroy data the user did not name; "removing" only the
link would leave the real OAuth state in place while claiming it was revoked), the filesystem root and the home directory (or any
parent of it) are never removed, and every error is surfaced: no `ignore_errors`.
PAST BUG: `end --revoke` used `rmtree(ignore_errors=True)`, so a symlinked or read-only data dir stayed intact and the command
still said "revoked" and exited 0.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from agenttools_errors import AgentToolError, PermissionDeniedError


def remove_tree(path: Path) -> bool:
    """Remove `path` (a directory or a file). Returns False when it did not exist; raises AgentToolError when it cannot be
    removed."""
    if path.is_symlink():
        raise AgentToolError(
            what=f"refusing to remove {path}: it is a symbolic link to {os.readlink(path)}",
            why="deleting through a link would either destroy files outside this target or leave the real data in place",
            fix=(
                f"point the setting at the real directory in config.toml (or remove the link {path} yourself), "
                "then run the command again"
            ),
        )
    resolved = path.resolve()
    home = Path(os.environ.get("HOME") or Path.home()).resolve()
    if resolved == Path(resolved.anchor) or resolved == home or resolved in home.parents:
        raise AgentToolError(
            what=f"refusing to remove {path}",
            why="that is the filesystem root, the home directory or one of its parents",
            fix="check the path settings in config.toml: they must name a dedicated directory",
        )
    if not path.exists():
        return False
    failures: list[str] = []

    def collect(function: object, failed: str, error: BaseException) -> None:
        del function
        failures.append(f"{failed}: {error}")

    if path.is_dir():
        shutil.rmtree(path, onexc=collect)
    else:
        try:
            path.unlink()
        except OSError as error:
            failures.append(f"{path}: {error}")
    if failures or path.exists():
        raise PermissionDeniedError(
            what=f"could not remove {path}",
            why="; ".join(failures[:3]) or "it still exists after the removal",
            fix=f"stop whatever uses it, fix the permissions and remove {path} yourself, then run the command again",
        )
    return True
