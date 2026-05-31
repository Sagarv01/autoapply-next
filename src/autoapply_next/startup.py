"""Startup-side lifecycle wiring for AutoApply Next.

This module owns the cross-cutting concerns that need to wrap the QApplication
lifetime so a long-running batch survives sleep, single-instance collisions,
and orphan rows from previous crashes:

1. ``CaffeinateManager`` spawns ``caffeinate -disu`` on macOS so the machine
   does not sleep mid-batch. No-op (with WARNING) on other platforms or when
   ``caffeinate`` is missing. atexit-clean and idempotent.

2. ``acquire_seek_lock()`` is a thin context-manager wrapper around the
   vendored ``process_lock.seek_lock`` that re-raises the underlying
   ``RuntimeError`` as :class:`SingleInstanceError` with a friendly message,
   so callers do not need to know the vendor exception type.

   IMPORTANT cwd quirk: vendor ``seek_lock`` uses a RELATIVE path
   (``sessions/seek/.lock``), so the caller must be in the engine workdir
   when the lock is acquired. ``acquire_seek_lock`` ``os.chdir(workdir)``
   before entering and restores the previous cwd on exit.

3. ``configure_rotating_log`` replaces the simple ``FileHandler`` in
   ``__main__`` with a ``RotatingFileHandler`` (10 MB x 5 files) so a
   long-running install does not silently fill the disk with log lines.

4. ``run_startup_recovery`` flips any ``in_progress`` rows (orphans from a
   crashed apply) to ``failed`` via ``persistence.recover_orphans``. Runs
   BEFORE the main window appears so the UI never shows a stale
   "in progress" row.

5. ``OrphanWatchdog`` is an optional periodic watchdog. Default off. When
   started, fires a ``QTimer`` and calls ``recover_orphans``. Used belt-and-
   braces for long-lived sessions where a sub-process crash might otherwise
   leave a row stuck in ``in_progress`` until the next launch.

The seek-lock import is the ONLY line that reaches into ``vendor/job-finder``.
The engine source itself is byte-for-byte off limits.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import subprocess
import sys
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from PySide6.QtCore import QObject, QTimer

if TYPE_CHECKING:
    from .engine.persistence import ReconcileResult

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- caffeinate


class CaffeinateManager:
    """Spawn ``caffeinate -disu`` for the QApplication lifetime so the
    Mac will not sleep mid-batch.

    Idempotent: calling :meth:`start` twice only spawns one subprocess.
    atexit-clean: a stop is registered on first start so even a hard exit
    path tears the child process down.

    On non-darwin platforms or when ``caffeinate`` is not on PATH, both
    ``start`` and ``stop`` become no-ops and :attr:`pid` stays ``None``.
    A WARNING is logged once when ``caffeinate`` is missing.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._proc: subprocess.Popen | None = None
        self._atexit_registered = False

    @property
    def pid(self) -> int | None:
        """The PID of the spawned ``caffeinate`` process, or ``None`` when
        not started, disabled, or unavailable on this platform."""
        if self._proc is None:
            return None
        return self._proc.pid

    def start(self) -> None:
        """Spawn ``caffeinate -disu`` if available; otherwise log a WARNING
        and continue. Calling twice is a no-op."""
        if not self._enabled:
            return
        if self._proc is not None:
            return  # already started; idempotent
        if sys.platform != "darwin":
            logger.info(
                "CaffeinateManager: non-darwin platform (%s); skipping",
                sys.platform,
            )
            return
        if shutil.which("caffeinate") is None:
            logger.warning(
                "CaffeinateManager: caffeinate not found on PATH; "
                "Mac may sleep during long batches"
            )
            return
        try:
            self._proc = subprocess.Popen(["caffeinate", "-disu"])
        except OSError as exc:
            logger.warning(
                "CaffeinateManager: failed to spawn caffeinate: %s", exc
            )
            self._proc = None
            return
        logger.info(
            "CaffeinateManager: started caffeinate (pid=%s); "
            "Mac will not sleep while the app runs",
            self._proc.pid,
        )
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True

    def stop(self) -> None:
        """Terminate the spawned ``caffeinate``. Safe to call when not
        started or after it has already exited."""
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
        except Exception as exc:
            logger.warning(
                "CaffeinateManager: error stopping caffeinate: %s", exc
            )
        finally:
            self._proc = None
            logger.info("CaffeinateManager: stopped")


# ----------------------------------------------------------- single instance


class SingleInstanceError(Exception):
    """Raised when another autoapply-next or job-finder process holds the
    vendor seek lock. Carries the lockfile path so callers can surface it."""

    def __init__(self, message: str, lock_path: Path | None = None) -> None:
        super().__init__(message)
        self.lock_path = lock_path


@contextmanager
def acquire_seek_lock(engine_workdir: Path | None = None) -> Iterator[None]:
    """Acquire the vendored Seek single-instance lock for the duration of
    the with-block.

    The vendor implementation (``vendor/job-finder/process_lock.py``) uses
    a RELATIVE path ``sessions/seek/.lock``, so the lock is anchored to the
    current working directory. To match the vendor's daemon, we set cwd to
    ``engine_workdir`` before acquiring and restore it on exit. If
    ``engine_workdir`` is ``None``, the current cwd is used as-is (test
    paths pass an explicit workdir).

    Re-raises the vendor's ``RuntimeError`` as :class:`SingleInstanceError`
    with a friendly message that includes the lockfile path.
    """
    # Import lazily so a partial vendor checkout does not crash unrelated
    # callers at module import time. vendor/job-finder lives one level
    # above this package's parent (REPO_ROOT/vendor/job-finder); production
    # runs (python -m autoapply_next) do not have it on sys.path, only the
    # tests do via their session-level prepend, so we add it here.
    _vendor = (
        Path(__file__).resolve().parent.parent.parent / "vendor" / "job-finder"
    )
    if _vendor.is_dir() and str(_vendor) not in sys.path:
        sys.path.insert(0, str(_vendor))
    from process_lock import LOCK_PATH, seek_lock  # type: ignore[import-not-found]

    previous_cwd: Path | None = None
    if engine_workdir is not None:
        previous_cwd = Path.cwd()
        os.chdir(str(engine_workdir))

    try:
        try:
            cm = seek_lock()
            cm.__enter__()
        except RuntimeError as exc:
            absolute_lock = (
                Path(engine_workdir) / LOCK_PATH
                if engine_workdir is not None
                else Path.cwd() / LOCK_PATH
            )
            raise SingleInstanceError(
                "AutoApply Next: another instance (or the job-finder "
                f"daemon) is already running. Refusing to start. "
                f"Lock file: {absolute_lock}. Underlying error: {exc}",
                lock_path=absolute_lock,
            ) from exc
        try:
            yield
        finally:
            cm.__exit__(None, None, None)
    finally:
        if previous_cwd is not None:
            try:
                os.chdir(str(previous_cwd))
            except OSError:
                pass


# ----------------------------------------------------------------- log rotation


def configure_rotating_log(log_path: Path) -> logging.Handler:
    """Replace the root logger's ``FileHandler`` with a
    ``RotatingFileHandler`` writing to ``log_path``.

    10 MB per file, up to 5 backups (so ~50 MB cap on log disk usage).
    Uses the same formatter as ``__main__``'s original ``FileHandler``.
    Existing ``FileHandler``s on the root logger are detached and closed.

    Returns the new handler so callers can wire it elsewhere if needed.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    # Detach any plain FileHandler (but keep StreamHandlers; we still want
    # stderr output). RotatingFileHandler is a subclass of FileHandler so we
    # check for the exact base class to avoid removing ourselves on a
    # repeated call.
    for existing in list(root.handlers):
        if (
            isinstance(existing, logging.FileHandler)
            and not isinstance(existing, RotatingFileHandler)
        ):
            root.removeHandler(existing)
            try:
                existing.close()
            except Exception:
                pass
    root.addHandler(handler)
    return handler


# ------------------------------------------------------------ startup recovery


def run_startup_recovery(engine_workdir: Path) -> "list[ReconcileResult]":
    """Flip any ``in_progress`` orphan rows in ``jobs.db`` to ``failed``.

    Called before the main window is constructed so the user never sees
    stale "in progress" rows from a previous crash. Delegates to
    ``persistence.recover_orphans`` (Workstream B). Logs one INFO line per
    orphan; returns the list of ``ReconcileResult`` for caller-side
    instrumentation.

    If ``recover_orphans`` is unavailable (e.g. Workstream B not landed
    yet), returns an empty list and logs a WARNING.
    """
    try:
        from .engine.persistence import recover_orphans
    except ImportError as exc:
        logger.warning(
            "run_startup_recovery: persistence.recover_orphans unavailable "
            "(%s); skipping orphan recovery",
            exc,
        )
        return []

    results = recover_orphans(engine_workdir=engine_workdir)
    for r in results:
        logger.info(
            "startup-recovery: %s %s -> %s (%s)",
            r.action,
            r.url,
            r.new_status,
            r.note or "no note",
        )
    if not results:
        logger.info("startup-recovery: no orphans found")
    return results


# ----------------------------------------------------------- orphan watchdog


class OrphanWatchdog(QObject):
    """Optional periodic watchdog that calls ``persistence.recover_orphans``
    on a Qt timer.

    Default off. When ``start()`` is called, a ``QTimer`` fires every
    ``interval_ms`` (default 15 minutes) and runs recovery. The watchdog
    surfaces one log line summarising the count of reconciled rows. Errors
    inside the recovery callback are logged and swallowed so the watchdog
    keeps running across a transient DB hiccup.

    The 15-minute age filter is enforced by passing
    ``min_age_seconds=15*60`` to ``persistence.recover_orphans`` on each
    tick; that guarantees the watchdog never races a real in-flight
    apply (which parks an ``in_progress`` row for ~3-5 minutes while
    Claude tailors and Seek's form fills). Without this filter the
    watchdog would force-fail the running job and rely on the apply's
    later write to overwrite it, which works for ``applied`` (UPDATE
    resets failure_count) but inflates failure_count on the failure
    path and can trigger a false permafail (>= ``PERMAFAIL_THRESHOLD``).
    Startup recovery, in contrast, calls ``recover_orphans`` with no
    age filter because any ``in_progress`` row after a process restart
    is definitionally a crash orphan from the previous process.
    """

    def __init__(
        self,
        engine_workdir: Path,
        interval_ms: int = 15 * 60 * 1000,
        min_age_seconds: int = 15 * 60,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine_workdir = Path(engine_workdir)
        self._interval_ms = int(interval_ms)
        self._min_age_seconds = int(min_age_seconds)
        self._timer: QTimer | None = None
        self._fired = 0

    @property
    def fired(self) -> int:
        """Number of times the watchdog has fired. Useful for tests."""
        return self._fired

    def start(self) -> None:
        """Begin firing on the configured interval. Idempotent."""
        if self._timer is not None:
            return
        self._timer = QTimer(self)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        logger.info(
            "OrphanWatchdog: started; interval=%d ms",
            self._interval_ms,
        )

    def stop(self) -> None:
        """Stop the timer. Safe to call when not started."""
        if self._timer is None:
            return
        self._timer.stop()
        self._timer.deleteLater()
        self._timer = None
        logger.info("OrphanWatchdog: stopped")

    def _tick(self) -> None:
        self._fired += 1
        try:
            from .engine.persistence import recover_orphans
        except ImportError as exc:
            logger.warning(
                "OrphanWatchdog: recover_orphans unavailable (%s)", exc
            )
            return
        try:
            results = recover_orphans(
                engine_workdir=self._engine_workdir,
                min_age_seconds=self._min_age_seconds,
            )
        except Exception as exc:
            logger.warning("OrphanWatchdog: recover_orphans raised: %s", exc)
            return
        if results:
            logger.info(
                "OrphanWatchdog: reconciled %d orphan row(s)", len(results)
            )
        # Silent on empty cycles so log churn stays low; tests can read
        # `fired` to confirm the timer woke up.
