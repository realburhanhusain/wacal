"""Enforce one writer process per database.

The double-booking guard is a process-wide lock plus a re-check plus a partial
unique index on (owner_id, start_utc). The index alone does not catch an
overlapping booking with a different start time, which variable service
durations produce routinely. So `uvicorn --workers 4` is not merely slower, it
is wrong: two workers can each see a slot as free and each insert.

Documenting that in a README is not a guard. This takes an exclusive advisory
lock on a file beside the database at startup, so the second process fails
loudly instead of quietly double booking.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class AlreadyRunning(RuntimeError):
    """Another process already holds the write lock for this database."""


def allow_multiprocess() -> bool:
    """Escape hatch for a deployment that has moved its guard elsewhere.

    Set this only once writes are protected by something that works across
    processes -- a Postgres range exclusion constraint, for instance.
    """
    return os.environ.get("WACAL_ALLOW_MULTIPROCESS", "").lower() in (
        "1", "true", "yes")


def lock_path(db_path: Path | str) -> Path:
    return Path(f"{db_path}.writer.lock")


def pid_path(db_path: Path | str) -> Path:
    """A readable note of who holds the lock.

    The lock file itself cannot carry this on Windows: msvcrt locking is
    mandatory, so the locked byte range is unreadable even to the owner
    through a second handle. Operators still need to know which process to
    look at, so the pid goes in a plain sibling file.
    """
    return Path(f"{db_path}.writer.pid")


class WriterLock:
    """An exclusive advisory lock on <database>.writer.lock.

    Held for the life of the process. Released on close, and by the operating
    system if the process dies, so a crash does not need manual cleanup.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = db_path
        self.path = lock_path(db_path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        # msvcrt.locking works from the current position, and "a+b" opens at
        # the end of the file.
        handle.seek(0)
        try:
            _lock_exclusive(handle)
        except OSError as exc:
            handle.close()
            raise AlreadyRunning(
                f"another process already holds {self.path}. wacal serialises "
                f"bookings with an in-process lock, so a second worker can "
                f"double book. Run one process, or set "
                f"WACAL_ALLOW_MULTIPROCESS=1 once writes are guarded across "
                f"processes."
            ) from exc
        self._handle = handle
        pid_path(self.db_path).write_text(str(os.getpid()), encoding="utf-8")

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None
            pid_path(self.db_path).unlink(missing_ok=True)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


if sys.platform == "win32":
    import msvcrt

    def _lock_exclusive(handle) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_exclusive(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def guard(db_path: Path | str) -> WriterLock | None:
    """Take the writer lock unless multiprocess operation was opted into."""
    if allow_multiprocess():
        return None
    if str(db_path) == ":memory:":
        # Nothing to share: an in-memory database belongs to one process by
        # definition. Tests use this.
        return None
    lock = WriterLock(db_path)
    lock.acquire()
    return lock
