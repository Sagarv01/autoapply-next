"""
File-based exclusive lock so only ONE script touches the Seek session at a time.

Background: today multiple Chrome instances ran in parallel (bot + audit +
diagnostic + check_applied), which Seek can fingerprint as automated activity.
Use this lock around any code path that opens a Seek browser context.

The lock is OS-level (fcntl.flock) — it auto-releases if the process dies,
so no stale-lock cleanup is needed.

Usage:
    with seek_lock():
        # open browser, do work
        ...
"""
import contextlib
import fcntl
import logging
import os
from pathlib import Path

LOCK_PATH = Path("sessions/seek/.lock")
log = logging.getLogger(__name__)


@contextlib.contextmanager
def seek_lock():
    """Acquire an exclusive Seek lock; raise RuntimeError if another process holds it."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fp = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        raise RuntimeError(
            f"Another Seek session is active (lock: {LOCK_PATH}). "
            f"Stop the other script first — running parallel browsers triggers "
            f"Seek's bot detection."
        )
    fp.write(str(os.getpid()))
    fp.flush()
    log.info(f"Acquired Seek lock (pid={os.getpid()})")
    try:
        yield
    finally:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fp.close()
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        log.info("Released Seek lock")
