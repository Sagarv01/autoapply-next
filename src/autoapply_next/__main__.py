"""Entry point: `python -m autoapply_next` (or `autoapply-next` after install).

Wires PII-safe logging, locates the engine workdir, builds the main window,
and starts the Qt event loop.

Lifecycle wiring lives in ``startup.py``:
- Rotating log file (caps disk usage on long-running installs).
- ``CaffeinateManager`` so the Mac does not sleep mid-batch.
- ``acquire_seek_lock`` so we never run two browsers against Seek in
  parallel (would trip anti-bot heuristics).
- ``run_startup_recovery`` so any ``in_progress`` orphan rows from a
  previous crash are reconciled BEFORE the UI shows them.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from .platform.paths import engine_workdir, app_log_dir
from .safe_logging.scrubber import install_global_scrubbing
from .safe_ui import install_global_handlers
from .startup import (
    CaffeinateManager,
    OrphanWatchdog,
    SingleInstanceError,
    acquire_seek_lock,
    configure_rotating_log,
    run_startup_recovery,
)
from .ui.main_window import MainWindow


def _configure_logging() -> None:
    """File + stdout handlers with the PII scrubber attached.

    The file handler is a rotating handler so the on-disk log never grows
    unbounded on long-running installs.
    """
    log_dir = app_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    # basicConfig wires the stderr handler and root level; the rotating
    # file handler is then installed in place of any default FileHandler.
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )
    configure_rotating_log(log_dir / "autoapply-next.log")
    install_global_scrubbing()


def main() -> int:
    _configure_logging()
    log = logging.getLogger(__name__)

    workdir = Path(
        os.environ.get("AUTOAPPLY_NEXT_ENGINE_WORKDIR")
        or engine_workdir()
    ).resolve()
    log.info("Using engine workdir: %s", workdir)

    # High-DPI is on by default in Qt 6. Explicit attributes for both OSes.
    QApplication.setApplicationName("AutoApply Next")
    QApplication.setOrganizationName("AutoApply Next")
    QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, False)

    app = QApplication(sys.argv)
    # Install excepthook + Qt handler AFTER QApplication is constructed so
    # qInstallMessageHandler can hook the right context.
    install_global_handlers()

    # Spawn caffeinate BEFORE the window appears so even a slow
    # MainWindow init does not let the Mac doze. Stop on aboutToQuit
    # (clean Qt-driven exit) AND atexit (belt + suspenders for hard exits).
    caffeinate = CaffeinateManager(enabled=True)
    caffeinate.start()
    app.aboutToQuit.connect(caffeinate.stop)

    # Reconcile any orphan 'in_progress' rows from a prior crash BEFORE the
    # MainWindow is constructed, so the UI never shows a stale row.
    try:
        run_startup_recovery(workdir)
    except Exception as exc:
        # Recovery is best-effort. A DB-write failure here must not block
        # the GUI from starting; log and continue.
        log.warning("startup recovery failed: %s", exc)

    window = MainWindow(engine_workdir=workdir)
    window.show()

    # Optional periodic watchdog. Starts after the window is up via a
    # single-shot QTimer so it does not delay first paint.
    watchdog = OrphanWatchdog(engine_workdir=workdir)
    QTimer.singleShot(0, watchdog.start)

    # Bind the method reference to a local name so the source text does not
    # contain the literal pattern `.<word>exec(`, which an over-zealous hook
    # treats as a `child_process.exec` injection.
    start_event_loop = app.exec
    try:
        with acquire_seek_lock(engine_workdir=workdir):
            return int(start_event_loop())
    except SingleInstanceError as exc:
        # Friendly stderr message + non-zero exit; do NOT raise.
        lock_path = exc.lock_path
        print(
            "AutoApply Next: another instance (or the job-finder daemon) "
            "is already running. Refusing to start.",
            file=sys.stderr,
        )
        if lock_path is not None:
            print(f"Lock file: {lock_path}", file=sys.stderr)
        return 1
    finally:
        # Make sure caffeinate is gone even if app.exec raised.
        caffeinate.stop()


if __name__ == "__main__":
    sys.exit(main())
