"""Contract tests for ``autoapply_next.startup``.

Covers the cross-cutting startup lifecycle that Workstream A owns:

- :class:`CaffeinateManager` (spawn / no-op / idempotency).
- :func:`configure_rotating_log` (rotation kicks in past 10 MB).
- :func:`acquire_seek_lock` (delegates to the vendor lock, raises
  :class:`SingleInstanceError` when held by another process).
- :func:`run_startup_recovery` (delegates to ``persistence.recover_orphans``
  and flips an ``in_progress`` row to ``failed``).
- :class:`OrphanWatchdog` (calls recover_orphans on a QTimer).
"""

from __future__ import annotations

import fcntl
import logging
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from autoapply_next.startup import (
    CaffeinateManager,
    OrphanWatchdog,
    SingleInstanceError,
    acquire_seek_lock,
    configure_rotating_log,
    run_startup_recovery,
)

# Vendor's process_lock.py is imported lazily inside acquire_seek_lock,
# so we need it on sys.path for the seek-lock-related tests. We add it
# once per session here so contract tests stay self-contained.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENDOR_ENGINE = _REPO_ROOT / "vendor" / "job-finder"
if _VENDOR_ENGINE.exists() and str(_VENDOR_ENGINE) not in sys.path:
    sys.path.insert(0, str(_VENDOR_ENGINE))


# ============================================================ caffeinate


def test_caffeinate_starts_when_available(monkeypatch):
    """When caffeinate exists on PATH and platform is darwin, start()
    spawns ``caffeinate -disu`` exactly once."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "autoapply_next.startup.shutil.which",
        lambda name: "/usr/bin/caffeinate" if name == "caffeinate" else None,
    )

    spawned: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            spawned.append(list(args))
            self.pid = 4242
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self._alive = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self._alive = False

    monkeypatch.setattr(
        "autoapply_next.startup.subprocess.Popen", FakePopen
    )

    cm = CaffeinateManager()
    assert cm.pid is None
    cm.start()
    assert spawned == [["caffeinate", "-disu"]]
    assert cm.pid == 4242
    cm.stop()
    assert cm.pid is None


def test_caffeinate_noop_when_missing(monkeypatch, caplog):
    """When caffeinate is not on PATH, start() is a no-op (no Popen call,
    pid stays None) and a WARNING is logged."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "autoapply_next.startup.shutil.which", lambda name: None
    )

    def boom(*args, **kwargs):
        raise AssertionError(
            "subprocess.Popen must not be called when caffeinate is missing"
        )

    monkeypatch.setattr("autoapply_next.startup.subprocess.Popen", boom)

    cm = CaffeinateManager()
    with caplog.at_level(logging.WARNING, logger="autoapply_next.startup"):
        cm.start()

    assert cm.pid is None
    assert any("caffeinate not found" in r.message for r in caplog.records)


def test_caffeinate_noop_on_non_darwin(monkeypatch):
    """Non-darwin platforms must not even probe for caffeinate; start()
    is a clean no-op."""
    monkeypatch.setattr(sys, "platform", "linux")

    def which_boom(*a, **k):
        raise AssertionError("must not probe shutil.which on linux")

    def popen_boom(*a, **k):
        raise AssertionError("must not spawn caffeinate on linux")

    monkeypatch.setattr("autoapply_next.startup.shutil.which", which_boom)
    monkeypatch.setattr("autoapply_next.startup.subprocess.Popen", popen_boom)

    cm = CaffeinateManager()
    cm.start()
    assert cm.pid is None


def test_caffeinate_idempotent_start_stop(monkeypatch):
    """Calling start() twice spawns exactly once. stop() before any
    start is a safe no-op. stop() called twice is also safe."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "autoapply_next.startup.shutil.which",
        lambda name: "/usr/bin/caffeinate",
    )

    spawn_count = {"n": 0}

    class FakePopen:
        def __init__(self, args, **kwargs):
            spawn_count["n"] += 1
            self.pid = 9999
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self._alive = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self._alive = False

    monkeypatch.setattr(
        "autoapply_next.startup.subprocess.Popen", FakePopen
    )

    cm = CaffeinateManager()
    cm.stop()  # safe before any start
    cm.start()
    cm.start()  # idempotent
    assert spawn_count["n"] == 1
    cm.stop()
    cm.stop()  # safe after stop
    assert cm.pid is None


def test_caffeinate_disabled_flag(monkeypatch):
    """enabled=False suppresses everything."""

    def boom(*a, **k):
        raise AssertionError("disabled CaffeinateManager must not spawn")

    monkeypatch.setattr("autoapply_next.startup.subprocess.Popen", boom)
    cm = CaffeinateManager(enabled=False)
    cm.start()
    assert cm.pid is None


# =========================================================== rotating log


def test_rotating_log_handler_attributes(tmp_path):
    """The handler reports the expected size and backup-count contract."""
    handler = configure_rotating_log(tmp_path / "x.log")
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5
    assert Path(handler.baseFilename) == tmp_path / "x.log"
    handler.close()
    logging.getLogger().removeHandler(handler)


def test_rotating_log_rotates_past_10mb(tmp_path):
    """Writing more than 10 MB through the handler triggers rotation,
    producing ``x.log.1``."""
    log_path = tmp_path / "x.log"
    handler = configure_rotating_log(log_path)

    test_logger = logging.getLogger("test_rotating_log_rotates_past_10mb")
    test_logger.handlers = []
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    # ~1 KB per line; 12 * 1024 lines is ~12 MB so rotation must fire.
    line = "x" * 1000
    for _ in range(12 * 1024):
        test_logger.info(line)
    handler.close()
    logging.getLogger().removeHandler(handler)

    assert (tmp_path / "x.log.1").exists(), (
        f"Expected rotation backup, only saw: "
        f"{sorted(tmp_path.iterdir())}"
    )


def test_rotating_log_replaces_existing_file_handler(tmp_path):
    """A pre-existing plain ``FileHandler`` on the root logger is removed
    when ``configure_rotating_log`` runs, so we do not double-write."""
    pre_existing = logging.FileHandler(tmp_path / "old.log")
    root = logging.getLogger()
    root.addHandler(pre_existing)
    try:
        handler = configure_rotating_log(tmp_path / "new.log")
        # The pre-existing plain FileHandler must be gone.
        plain_file_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.FileHandler)
            and not isinstance(
                h, logging.handlers.RotatingFileHandler  # type: ignore[attr-defined]
            )
        ]
        assert plain_file_handlers == []
        # And the new rotating handler is registered.
        assert handler in root.handlers
    finally:
        # Clean up so other tests do not inherit our handler.
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass


# ============================================================ seek_lock


def _vendor_lock_path(workdir: Path) -> Path:
    """Recreate the relative path the vendor uses."""
    return workdir / "sessions" / "seek" / ".lock"


def test_acquire_seek_lock_holds_and_releases(tmp_path):
    """Entering the context creates the lockfile; exiting removes it."""
    lock_file = _vendor_lock_path(tmp_path)
    assert not lock_file.exists()

    with acquire_seek_lock(engine_workdir=tmp_path):
        # Inside the block the lockfile is present.
        assert lock_file.exists()

    # On clean exit the vendor unlinks the lockfile.
    assert not lock_file.exists()


def test_acquire_seek_lock_blocks_second_instance(tmp_path):
    """Simulate another process holding the lock: open the lockfile and
    take an exclusive fcntl flock, then attempt acquire and assert
    SingleInstanceError fires within ~1 s."""
    lock_file = _vendor_lock_path(tmp_path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    holder = open(lock_file, "w")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        with pytest.raises(SingleInstanceError) as exc_info:
            with acquire_seek_lock(engine_workdir=tmp_path):
                pytest.fail("acquire must not have entered the with-block")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"took too long to detect held lock: {elapsed}s"
        # Error must include the lockfile path so the user can find it.
        assert exc_info.value.lock_path is not None
        assert str(lock_file) in str(exc_info.value.lock_path)
    finally:
        try:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        holder.close()
        try:
            lock_file.unlink()
        except FileNotFoundError:
            pass


def test_acquire_seek_lock_restores_cwd(tmp_path):
    """``acquire_seek_lock`` must restore the original cwd on exit, even
    after a clean acquire/release cycle."""
    original_cwd = Path.cwd()
    try:
        with acquire_seek_lock(engine_workdir=tmp_path):
            # Inside the block we expect to be cwd-anchored to tmp_path
            # so the vendor's RELATIVE lock path resolves correctly.
            assert Path.cwd().resolve() == tmp_path.resolve()
        # On exit, cwd is back where we found it.
        assert Path.cwd().resolve() == original_cwd.resolve()
    finally:
        # Belt and braces.
        import os as _os
        _os.chdir(str(original_cwd))


# ====================================================== startup recovery


def _make_jobs_db(workdir: Path) -> Path:
    """Stand up an engine-shaped applications table in workdir/jobs.db."""
    db = workdir / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE applications ("
            "url TEXT PRIMARY KEY, title TEXT, company TEXT, board TEXT, "
            "match_score INTEGER, match_reasoning TEXT, resume_file TEXT, "
            "cover_letter_file TEXT, status TEXT, notes TEXT, "
            "timestamp TEXT, failure_count INTEGER)"
        )
    return db


def _seed_row(db: Path, url: str, status: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(url, title, company, board, match_score, status, "
            " timestamp, failure_count) VALUES (?,?,?,?,?,?,?,0)",
            (url, "T", "C", "seek", 70, status, "t"),
        )


def _read_status(db: Path, url: str) -> str | None:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status FROM applications WHERE url = ?", (url,)
        ).fetchone()
        return row[0] if row else None


def test_run_startup_recovery_flips_in_progress_to_failed(
    tmp_path, monkeypatch
):
    """``run_startup_recovery`` calls persistence.recover_orphans (the API
    Workstream B owns). We stub it here so this contract test does not
    depend on B's landing order; the wiring is what we are pinning.

    A separate integration test in B's workstream pins the actual DB
    transition.
    """
    url = "https://au.seek.com/job/123"
    db = _make_jobs_db(tmp_path)
    _seed_row(db, url, "in_progress")

    calls: list[dict] = []

    def fake_recover(*, engine_workdir, **_):
        calls.append({"engine_workdir": engine_workdir})
        # Mimic B's behaviour: flip in_progress to failed.
        with sqlite3.connect(engine_workdir / "jobs.db") as conn:
            conn.execute(
                "UPDATE applications SET status = 'failed' "
                "WHERE status = 'in_progress'"
            )
            conn.commit()

        class _R:
            url = "https://au.seek.com/job/123"
            prior_status = "in_progress"
            new_status = "failed"
            action = "force_failed"
            note = "orphan from crash"

        return [_R()]

    # Inject a fake recover_orphans into the persistence module.
    import autoapply_next.engine.persistence as persistence_mod

    monkeypatch.setattr(
        persistence_mod, "recover_orphans", fake_recover, raising=False
    )

    results = run_startup_recovery(tmp_path)

    assert len(calls) == 1
    assert calls[0]["engine_workdir"] == tmp_path
    assert len(results) == 1
    assert results[0].action == "force_failed"
    # And the DB actually transitioned.
    assert _read_status(db, url) == "failed"


def test_run_startup_recovery_handles_missing_api(tmp_path, monkeypatch, caplog):
    """If ``persistence.recover_orphans`` is unavailable (Workstream B not
    landed yet), we return an empty list and log a WARNING; we do not
    raise. This is the graceful-degradation safety net."""
    import autoapply_next.engine.persistence as persistence_mod

    # Pretend the attribute was never defined.
    if hasattr(persistence_mod, "recover_orphans"):
        monkeypatch.delattr(persistence_mod, "recover_orphans")

    # Also patch the import machinery: from .engine.persistence import
    # recover_orphans must raise ImportError. We achieve this by deleting
    # the attribute then forcing the from-import to re-resolve through
    # __getattr__, which won't be defined here. Easiest path: monkeypatch
    # the import statement target by replacing the module-level binding.
    # Since `from x import y` raises ImportError if y missing, deleting
    # the attribute is sufficient.

    out = run_startup_recovery(tmp_path)
    assert out == []


# ============================================================= watchdog


def test_orphan_watchdog_runs_on_timer(tmp_path, monkeypatch, qtbot):
    """Instantiate OrphanWatchdog with a tiny interval and assert that
    ``recover_orphans`` is called via the QTimer within ~200 ms."""
    import autoapply_next.engine.persistence as persistence_mod

    calls: list[Path] = []

    def fake_recover(*, engine_workdir, **_):
        calls.append(engine_workdir)
        return []

    monkeypatch.setattr(
        persistence_mod, "recover_orphans", fake_recover, raising=False
    )

    watchdog = OrphanWatchdog(engine_workdir=tmp_path, interval_ms=50)
    watchdog.start()
    try:
        qtbot.waitUntil(lambda: len(calls) >= 1, timeout=2000)
    finally:
        watchdog.stop()

    assert calls[0] == tmp_path
    assert watchdog.fired >= 1


def test_orphan_watchdog_swallows_errors(tmp_path, monkeypatch, qtbot):
    """If ``recover_orphans`` raises, the watchdog logs and continues
    firing instead of crashing the event loop."""
    import autoapply_next.engine.persistence as persistence_mod

    n_calls = {"n": 0}

    def fake_recover(*, engine_workdir, **_):
        n_calls["n"] += 1
        raise RuntimeError("simulated db hiccup")

    monkeypatch.setattr(
        persistence_mod, "recover_orphans", fake_recover, raising=False
    )

    watchdog = OrphanWatchdog(engine_workdir=tmp_path, interval_ms=30)
    watchdog.start()
    try:
        qtbot.waitUntil(lambda: n_calls["n"] >= 2, timeout=2000)
    finally:
        watchdog.stop()

    assert n_calls["n"] >= 2  # kept firing despite the raise


def test_orphan_watchdog_idempotent_start_stop(tmp_path):
    """start() twice is a no-op; stop() before start is safe."""
    watchdog = OrphanWatchdog(engine_workdir=tmp_path, interval_ms=60_000)
    watchdog.stop()  # safe before start
    watchdog.start()
    watchdog.start()  # idempotent
    watchdog.stop()
    watchdog.stop()  # safe after stop


def test_orphan_watchdog_passes_min_age_seconds(tmp_path, monkeypatch, qtbot):
    """Regression: the watchdog must forward ``min_age_seconds`` to
    ``recover_orphans`` so it never races a live in-flight apply that
    parked an ``in_progress`` row a few minutes ago. Without this the
    watchdog flips the running row, the apply overwrites it on success,
    and failure_count is silently inflated on the way through. Default
    is 15*60 (matches OrphanWatchdog.__init__)."""
    import autoapply_next.engine.persistence as persistence_mod

    seen_kwargs: list[dict] = []

    def fake_recover(*, engine_workdir, **kw):
        seen_kwargs.append(kw)
        return []

    monkeypatch.setattr(
        persistence_mod, "recover_orphans", fake_recover, raising=False
    )

    watchdog = OrphanWatchdog(engine_workdir=tmp_path, interval_ms=50)
    watchdog.start()
    try:
        qtbot.waitUntil(lambda: len(seen_kwargs) >= 1, timeout=2000)
    finally:
        watchdog.stop()

    assert seen_kwargs[0].get("min_age_seconds") == 15 * 60
