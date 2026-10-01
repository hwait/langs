"""Where a running server records itself, and the credential it never records.

The port has to be discoverable: a learner runs `client serve` in one terminal and the CLI
in another, and a page the launch command opened needs to come back to the same process.
The token must not be, because ADR 0008 introduces no persistent credential store -- it is
minted per start, handed to the page through the URL fragment, and lives in memory.

`data/` is already in `paths.PRIVATE_DIRECTORIES` and in the rendered gitignore, so nothing
here reaches the privacy audit that was not already covered.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from linguawiki import __version__
from linguawiki.clock import Clock
from linguawiki.paths import WorkspacePaths

RUNTIME_FILE_NAME = "client-runtime.json"

#: The runtime record's own version, so a later shape change can be recognized rather than
#: guessed at by a reader that finds unexpected keys.
RUNTIME_VERSION = 1


def runtime_path(paths: WorkspacePaths) -> Path:
    return paths.database.parent / RUNTIME_FILE_NAME


def mint_token() -> str:
    """A launch token for one server start.

    `token_urlsafe(32)` is 256 bits of entropy, which is far more than a loopback page
    needs and exactly as much as it costs.
    """

    return secrets.token_urlsafe(32)


def write(paths: WorkspacePaths, *, port: int, clock: Clock) -> Path:
    """Record the running server, replacing any record of one that is no longer there.

    A port claimed by nothing is a worse answer than no answer, so a stale record is
    overwritten rather than respected. The check is `os.kill(pid, 0)`: it asks whether the
    process exists without touching it, and a `PermissionError` means it exists and belongs
    to somebody else -- which is still an answer, so it counts as alive.
    """

    path = runtime_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "runtime_version": RUNTIME_VERSION,
        "application_version": __version__,
        "port": port,
        "pid": os.getpid(),
        "started_at": clock.now().isoformat().replace("+00:00", "Z"),
    }
    # No token. Writing it would turn a per-start credential into a stored one, and the
    # whole point of the fragment hand-off is that it is never at rest.
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read(paths: WorkspacePaths) -> dict[str, object] | None:
    """The recorded server, or `None` when there is no readable record of a live one."""

    path = runtime_path(paths)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Unreadable is the same answer as absent for a caller asking "where do I connect":
        # it does not know, and reporting a crash instead of "nowhere" helps nobody.
        return None
    if not isinstance(document, dict) or not isinstance(document.get("pid"), int):
        return None
    return document if _alive(int(document["pid"])) else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def clear(paths: WorkspacePaths) -> None:
    """Remove the record. Absent is the state a stopped server leaves behind."""

    runtime_path(paths).unlink(missing_ok=True)


__all__ = [
    "RUNTIME_FILE_NAME",
    "RUNTIME_VERSION",
    "clear",
    "mint_token",
    "read",
    "runtime_path",
    "write",
]
