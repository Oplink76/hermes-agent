"""Cross-platform non-blocking file locks for destructive operations."""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _contention(error: OSError) -> bool:
    if isinstance(error, BlockingIOError):
        return True
    return os.name == "nt" and (
        error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
        or getattr(error, "winerror", None) in {33, 36}
    )


@contextmanager
def try_exclusive_file_lock(path: Path) -> Iterator[bool]:
    """Yield whether ``path`` was locked; propagate non-contention failures."""
    lock_path = Path(path).expanduser().resolve(strict=False)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+b") as handle:
        if os.name != "nt":
            os.chmod(lock_path, 0o600)
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if not _contention(exc):
                raise
            yield False
            return

        try:
            yield True
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
