"""Cross-process file locks (POSIX ``flock``; a no-op where unavailable, e.g. Windows)."""

import contextlib
from pathlib import Path
from typing import IO, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def try_lock(path: Path) -> Optional[IO]:
    """Take an exclusive lock on ``path`` without waiting.

    Returns the open file (keep it referenced: the lock is released when it is closed or
    the process exits) or ``None`` if another process already holds the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    if fcntl is None:
        return handle
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for the duration of the ``with`` block (waits)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
