"""First-run / empty-state safety: no config, no profile, no session, empty DB.

The MainWindow is constructed against a tmp_path that contains nothing the
engine workdir conventionally has. Every screen must render without
crashing. Buttons that require missing inputs must surface a visible error,
not silently do nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from autoapply_next.engine.results import ApplicationResult, ApplicationStatus
from autoapply_next.engine.scraping import ScrapeResult
from autoapply_next.engine.session_bootstrap import (
    SessionBootstrapResult,
    SessionStatus,
)


@pytest.fixture
def fresh_workdir(tmp_path) -> Path:
    # Deliberately empty: no config.yaml, no assets/, no sessions/, no jobs.db.
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_engine(monkeypatch):
    import autoapply_next.engine.worker as wm

    async def fake_apply(**kwargs):
        return ApplicationResult(
            job_url=kwargs["job_url"], status=ApplicationStatus.DRY_RUN_VERIFIED
        )

    async def fake_session(**kwargs):
        return SessionBootstrapResult(status=SessionStatus.VALID, message="ok")

    async def fake_scrape(**kwargs):
        return ScrapeResult(
            keyword=kwargs["keyword"],
            total_scraped=0, new_jobs=0, scored=[], errors=[],
        )

    monkeypatch.setattr(wm, "apply_to_job", fake_apply)
    monkeypatch.setattr(wm, "run_session_bootstrap", fake_session)
    monkeypatch.setattr(wm, "scrape_and_score", fake_scrape)


def _close_all_messageboxes():
    from autoapply_next.safe_ui import error_handler as _eh

    for w in QApplication.topLevelWidgets():
        if isinstance(w, QMessageBox):
            w.close()
    _eh._open_dialogs.clear()


def _any_messagebox() -> bool:
    from autoapply_next.safe_ui import error_handler as _eh

    for w in QApplication.topLevelWidgets():
        if isinstance(w, QMessageBox):
            return True
    return bool(_eh._open_dialogs)


def test_main_window_boots_on_empty_workdir(qtbot, fresh_workdir):
    """The most basic empty-state guarantee: MainWindow constructs without
    crashing against an empty directory."""
    from autoapply_next.ui.main_window import MainWindow

    win = MainWindow(engine_workdir=fresh_workdir)
    qtbot.addWidget(win)
    win.show()
    qtbot.wait(40)
    assert win.isVisible()
    # Visit every screen via the toolbar shortcut actions; none may crash.
    for action in win._actions.values():
        action.trigger()
        qtbot.wait(20)
    win.close()
    # worker is stopped by closeEvent; ensure no lingering errors.
    _close_all_messageboxes()


def test_failed_session_restore_is_handled_and_gate_stays_consistent(qtbot, fresh_workdir):
    """A non-AuthError during the off-thread session restore must not leave an
    unhandled failed-signal; the gate is re-applied (and stays locked since the
    user is not signed in). An unrelated screen token must be ignored."""
    from autoapply_next.ui.main_window import MainWindow

    win = MainWindow(engine_workdir=fresh_workdir)
    qtbot.addWidget(win)
    # Simulate a failed restore (e.g. keychain or transient refresh error).
    win._on_runner_failed("RuntimeError: keychain boom", "auth_restore")
    # Bot stays locked on a fresh, signed-out workdir; no crash.
    assert not win._actions[win._queue].isEnabled()
    # A different screen's failure token is ignored here (the screen handles it).
    win._on_runner_failed("whatever", "profile_save")
    win.close()


def test_below_version_floor_walls_the_app(qtbot, fresh_workdir, monkeypatch):
    import autoapply_next.ui.main_window as mw
    from autoapply_next.ui.main_window import MainWindow

    win = MainWindow(engine_workdir=fresh_workdir)
    qtbot.addWidget(win)
    # The running build is 1.0.0; the proxy floor came back as 9.9.9.
    monkeypatch.setattr(mw, "_client_version", lambda: "1.0.0")
    win._on_runner_succeeded("9.9.9", "version_check")
    assert win._stack.currentWidget() is win._update_screen
    # the wall disables navigation
    assert not win._actions[win._queue].isEnabled()
    win.close()


def test_run_screen_empty_workdir_validates(qtbot, fresh_workdir):
    """Clicking Run with no URL and no engine on a tester's first launch must
    open a dialog, not just sit there."""
    from autoapply_next.ui.run_screen import RunScreen
    from autoapply_next.ui.settings_store import SettingsStore
    from autoapply_next.engine.worker import EngineWorker

    settings = SettingsStore(fresh_workdir / "ui-settings.json")
    worker = EngineWorker(engine_workdir=fresh_workdir)
    try:
        screen = RunScreen(worker=worker, settings=settings)
        qtbot.addWidget(screen)
        _close_all_messageboxes()
        screen._on_run_clicked()
        qtbot.wait(30)
        # The URL is empty by default on a fresh launch -> validation dialog.
        assert _any_messagebox(), "empty URL on first launch must surface a dialog"
        _close_all_messageboxes()
    finally:
        worker.stop_loop()


def test_profile_screen_empty_workdir_renders(qtbot, fresh_workdir):
    """Profile screen has to read config.yaml + assets/profile.txt. With
    neither present, it must render with empty fields, not crash."""
    from autoapply_next.ui.profile_screen import ProfileScreen

    screen = ProfileScreen(engine_workdir=fresh_workdir)
    qtbot.addWidget(screen)
    assert screen._name.text() == ""
    assert screen._email.text() == ""
    assert screen._phone.text() == ""
    assert screen._resume.toPlainText() == ""


def test_queue_screen_empty_workdir_renders(qtbot, fresh_workdir):
    from autoapply_next.engine.worker import EngineWorker
    from autoapply_next.ui.queue_screen import QueueScreen
    from autoapply_next.ui.settings_store import SettingsStore

    settings = SettingsStore(fresh_workdir / "ui-settings.json")
    worker = EngineWorker(engine_workdir=fresh_workdir)
    try:
        screen = QueueScreen(
            engine_workdir=fresh_workdir, worker=worker, settings=settings
        )
        qtbot.addWidget(screen)
        assert screen._table.rowCount() == 0
        # The count_label should explain the empty state.
        assert screen._count_label.text() != ""
    finally:
        worker.stop_loop()


def test_results_screen_empty_workdir_renders(qtbot, fresh_workdir):
    from autoapply_next.ui.results_screen import ResultsScreen

    screen = ResultsScreen(engine_workdir=fresh_workdir)
    qtbot.addWidget(screen)
    assert screen._table.rowCount() == 0


def test_session_screen_empty_workdir_renders(qtbot, fresh_workdir):
    from autoapply_next.engine.worker import EngineWorker
    from autoapply_next.ui.session_setup_screen import SessionSetupScreen

    worker = EngineWorker(engine_workdir=fresh_workdir)
    try:
        screen = SessionSetupScreen(engine_workdir=fresh_workdir, worker=worker)
        qtbot.addWidget(screen)
        # Status banner must say "No Seek session" since no profile dir.
        assert "No Seek session" in screen._status_label.text()
    finally:
        worker.stop_loop()
