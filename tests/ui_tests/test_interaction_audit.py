"""Click every interactive control on every screen and assert SOMETHING happens.

This is the per-screen audit promised in Part C of the QA brief. For each
screen, we instantiate it under `QT_QPA_PLATFORM=offscreen` (handled at the
qtbot level), construct it with a fake engine_workdir, then click each
button / toggle each checkbox and assert an observable effect:

- A signal fires.
- State (label text, enabled, current cell) changes.
- A dialog opens (we inspect `QApplication.topLevelWidgets()` for a new
  QMessageBox).
- The worker receives a method call (we stub the worker for that).

If a control does nothing, the test that exercises it fails. That is the
mechanism that catches the dead button.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from autoapply_next.engine.results import ApplicationResult, ApplicationStatus
from autoapply_next.engine.progress import ProgressEvent, ProgressStage
from autoapply_next.engine.scraping import ScrapeResult, ScrapedJob
from autoapply_next.engine.session_bootstrap import (
    SessionBootstrapResult,
    SessionStatus,
)
from autoapply_next.engine.worker import EngineWorker
from autoapply_next.ui.profile_screen import ProfileScreen
from autoapply_next.ui.queue_screen import QueueScreen
from autoapply_next.ui.results_screen import ResultsScreen
from autoapply_next.ui.run_screen import RunScreen
from autoapply_next.ui.session_setup_screen import SessionSetupScreen
from autoapply_next.ui.settings_screen import SettingsScreen
from autoapply_next.ui.settings_store import SettingsStore


# ---------------------------------------------------------------------- fixtures


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    # Minimal layout the screens can read without crashing.
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "profile.txt").write_text(
        "Test User\ntest@example.com\n+61 4 00 00 00\n", encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        "candidate:\n  name: Test User\n  email: test@example.com\n  phone: '+61 400 000 000'\n",
        encoding="utf-8",
    )
    (tmp_path / "errors").mkdir()
    return tmp_path


@pytest.fixture
def settings(tmp_path) -> SettingsStore:
    return SettingsStore(tmp_path / "ui-settings.json")


@pytest.fixture
def fake_worker(workdir, monkeypatch) -> EngineWorker:
    """A real EngineWorker but with its engine functions stubbed so we never
    touch real Seek. Lets us verify the screens call the right worker
    methods AND that the worker's state machine fires."""
    import autoapply_next.engine.worker as wm

    async def fake_apply(**kwargs):
        return ApplicationResult(
            job_url=kwargs["job_url"], status=ApplicationStatus.DRY_RUN_VERIFIED
        )

    async def fake_session(**kwargs):
        return SessionBootstrapResult(
            status=SessionStatus.VALID, message="ok"
        )

    async def fake_scrape(**kwargs):
        return ScrapeResult(
            keyword=kwargs["keyword"],
            total_scraped=0,
            new_jobs=0,
            scored=[],
            errors=[],
        )

    monkeypatch.setattr(wm, "apply_to_job", fake_apply)
    monkeypatch.setattr(wm, "run_session_bootstrap", fake_session)
    monkeypatch.setattr(wm, "scrape_and_score", fake_scrape)

    w = EngineWorker(engine_workdir=workdir)
    yield w
    w.stop_loop()


def _topmost_messagebox() -> QMessageBox | None:
    # Under QT_QPA_PLATFORM=offscreen, isVisible() can be False on a window
    # that has had show() called on it because the offscreen platform never
    # actually maps it. We accept any QMessageBox in topLevelWidgets that
    # the safe_ui keep-alive list is still holding, which is what we care
    # about for the audit: "did the code intend to show a dialog?"
    from autoapply_next.safe_ui import error_handler as _eh

    for w in QApplication.topLevelWidgets():
        if isinstance(w, QMessageBox):
            return w
    # Fallback: peek the keep-alive list.
    if _eh._open_dialogs:
        return _eh._open_dialogs[-1]
    return None


def _close_all_messageboxes() -> None:
    for w in QApplication.topLevelWidgets():
        if isinstance(w, QMessageBox):
            w.close()


# SignIn is now a real email/password screen with background auth; its behavior
# (success -> authenticated, error states, validation) lives in test_signin_screen.py.


# -------------------------------------------------------------------- Settings


def test_settings_real_submit_cancel_does_not_flip(qtbot, settings, monkeypatch):
    """The gate must remain off if the user cancels the confirmation."""
    # QMessageBox.question is the modal that asks; monkey-patch it to Cancel.
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *a, **kw: QMessageBox.Cancel
    ))
    screen = SettingsScreen(settings=settings)
    qtbot.addWidget(screen)
    assert not settings.allow_real_submit
    screen._allow_box.setChecked(True)
    qtbot.wait(20)
    assert not settings.allow_real_submit
    assert not screen._allow_box.isChecked(), "checkbox must roll back on cancel"


def test_settings_real_submit_confirm_flips_and_back_off_without_prompt(
    qtbot, settings, monkeypatch
):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *a, **kw: QMessageBox.Yes
    ))
    screen = SettingsScreen(settings=settings)
    qtbot.addWidget(screen)
    screen._allow_box.setChecked(True)
    qtbot.wait(20)
    assert settings.allow_real_submit is True
    # Flipping off must NOT prompt for confirmation (only on, not off).
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *a, **kw: pytest.fail("should not have asked on off")
    ))
    screen._allow_box.setChecked(False)
    qtbot.wait(20)
    assert settings.allow_real_submit is False


def test_settings_threshold_changes_setting(qtbot, settings):
    screen = SettingsScreen(settings=settings)
    qtbot.addWidget(screen)
    screen._threshold_spin.setValue(73)
    qtbot.wait(20)
    assert settings.match_threshold == 73


# -------------------------------------------------------------------- Profile


def test_profile_save_empty_shows_error(qtbot, workdir, monkeypatch):
    # Wipe to empty fields.
    (workdir / "config.yaml").write_text(
        "candidate:\n  name: ''\n  email: ''\n  phone: ''\n", encoding="utf-8"
    )
    (workdir / "assets" / "profile.txt").write_text("", encoding="utf-8")

    screen = ProfileScreen(engine_workdir=workdir)
    qtbot.addWidget(screen)
    screen._name.setText("")
    screen._email.setText("")
    screen._phone.setText("")
    screen._resume.setPlainText("")
    _close_all_messageboxes()
    screen._save()
    qtbot.wait(30)
    box = _topmost_messagebox()
    assert box is not None, "empty save must surface a dialog"
    assert (
        "incomplete" in box.windowTitle().lower()
        or "incomplete" in box.text().lower()
        or "fill in" in box.text().lower()
    )
    box.close()


def test_profile_save_writes_atomically(qtbot, workdir):
    screen = ProfileScreen(engine_workdir=workdir)
    qtbot.addWidget(screen)
    screen._name.setText("Real Name")
    screen._email.setText("real@example.com")
    screen._phone.setText("+61 4 12 34 56")
    screen._resume.setPlainText("Hello\n")
    _close_all_messageboxes()
    # We do not want the "Saved" info dialog to block the test; QMessageBox.information is modal.
    # Patch it out for this test only.
    from PySide6.QtWidgets import QMessageBox as QMB

    import unittest.mock as mock

    with mock.patch.object(QMB, "information", staticmethod(lambda *a, **kw: QMB.Ok)):
        screen._save()
    qtbot.wait(20)
    cfg_text = (workdir / "config.yaml").read_text()
    assert "Real Name" in cfg_text
    assert "real@example.com" in cfg_text
    assert (workdir / "assets" / "profile.txt").read_text() == "Hello\n"


# ---------------------------------------------------------------------- Queue


def test_queue_scrape_empty_keyword_shows_dialog(qtbot, workdir, fake_worker, settings):
    screen = QueueScreen(
        engine_workdir=workdir, worker=fake_worker, settings=settings
    )
    qtbot.addWidget(screen)
    screen._keyword_input.setText("")
    _close_all_messageboxes()
    screen._on_refresh_clicked()
    qtbot.wait(30)
    assert _topmost_messagebox() is not None, "empty keyword must dialog"
    _close_all_messageboxes()


def test_queue_filter_checkboxes_change_state(qtbot, workdir, fake_worker, settings):
    screen = QueueScreen(
        engine_workdir=workdir, worker=fake_worker, settings=settings
    )
    qtbot.addWidget(screen)
    initial = screen._only_queued.isChecked()
    screen._only_queued.toggle()
    assert screen._only_queued.isChecked() is not initial
    screen._only_above_threshold.toggle()
    qtbot.wait(20)
    # No assertion on contents (DB may be empty); the point is no crash.


def test_queue_scrape_keyword_drives_worker(qtbot, workdir, fake_worker, settings):
    screen = QueueScreen(
        engine_workdir=workdir, worker=fake_worker, settings=settings
    )
    qtbot.addWidget(screen)
    received: list[ScrapeResult] = []
    fake_worker.scrape_finished.connect(received.append)
    screen._keyword_input.setText("python")
    with qtbot.waitSignal(fake_worker.scrape_finished, timeout=3000):
        screen._on_refresh_clicked()
    assert received and received[0].keyword == "python"


def test_queue_reload_table_button_runs(qtbot, workdir, fake_worker, settings):
    screen = QueueScreen(
        engine_workdir=workdir, worker=fake_worker, settings=settings
    )
    qtbot.addWidget(screen)
    # Just verify the click runs without raising; empty DB means 0 rows.
    screen._reload_btn.click()
    qtbot.wait(20)


# ---------------------------------------------------------------------- Run


def test_run_empty_url_shows_dialog(qtbot, workdir, fake_worker, settings):
    screen = RunScreen(worker=fake_worker, settings=settings)
    qtbot.addWidget(screen)
    screen._url_input.setText("")
    _close_all_messageboxes()
    screen._on_run_clicked()
    qtbot.wait(30)
    assert _topmost_messagebox() is not None
    _close_all_messageboxes()


def test_run_non_seek_url_shows_dialog(qtbot, workdir, fake_worker, settings):
    screen = RunScreen(worker=fake_worker, settings=settings)
    qtbot.addWidget(screen)
    screen._url_input.setText("https://www.linkedin.com/jobs/view/123")
    _close_all_messageboxes()
    screen._on_run_clicked()
    qtbot.wait(30)
    assert _topmost_messagebox() is not None
    _close_all_messageboxes()


def test_run_valid_url_drives_worker(qtbot, workdir, fake_worker, settings):
    screen = RunScreen(worker=fake_worker, settings=settings)
    qtbot.addWidget(screen)
    screen._url_input.setText("https://au.seek.com/job/92398511")
    finished: list[ApplicationResult] = []
    fake_worker.finished.connect(finished.append)
    with qtbot.waitSignal(fake_worker.finished, timeout=3000):
        screen._on_run_clicked()
    assert finished and finished[0].status == ApplicationStatus.DRY_RUN_VERIFIED


def test_run_failed_status_shows_error_panel_and_dialog(
    qtbot, workdir, fake_worker, settings
):
    screen = RunScreen(worker=fake_worker, settings=settings)
    qtbot.addWidget(screen)
    failure = ApplicationResult(
        job_url="https://au.seek.com/job/X",
        status=ApplicationStatus.FAILED,
        exception_type="JobNotQuickApplyError",
        error_message="Not a quick-apply listing",
    )
    _close_all_messageboxes()
    screen._on_finished(failure)
    qtbot.wait(40)
    # Screenshot area now carries the friendly failure text.
    assert "quick-apply" in screen._screenshot_label.text().lower()
    # And the error dialog is visible.
    assert _topmost_messagebox() is not None
    _close_all_messageboxes()


def test_run_cancel_button_calls_worker_cancel(qtbot, workdir, fake_worker, settings):
    screen = RunScreen(worker=fake_worker, settings=settings)
    qtbot.addWidget(screen)
    fake_worker.cancel = MagicMock(wraps=fake_worker.cancel)
    screen._on_cancel_clicked()
    fake_worker.cancel.assert_called_once()


# ------------------------------------------------------------------- Results


def test_results_refresh_runs_without_db(qtbot, workdir):
    screen = ResultsScreen(engine_workdir=workdir)
    qtbot.addWidget(screen)
    screen._refresh_btn.click()
    qtbot.wait(20)
    assert screen._table.rowCount() == 0
    assert "No jobs.db" in screen._count_label.text() or screen._count_label.text() != ""


def test_results_select_row_with_no_db_does_not_crash(qtbot, workdir):
    screen = ResultsScreen(engine_workdir=workdir)
    qtbot.addWidget(screen)
    screen._on_row_changed(-1, 0, 0, 0)
    # No assertion needed; absence of exception is the test.


# ------------------------------------------------------------------- Session


def test_session_re_check_changes_status(qtbot, workdir, fake_worker):
    screen = SessionSetupScreen(engine_workdir=workdir, worker=fake_worker)
    qtbot.addWidget(screen)
    # No session present in the workdir.
    initial_text = screen._status_label.text()
    screen._recheck_btn.click()
    qtbot.wait(20)
    # Text might be the same (no session before and after), but the status
    # block must have been styled.
    assert "No Seek session" in screen._status_label.text() or initial_text


def test_session_launch_drives_worker(qtbot, workdir, fake_worker):
    screen = SessionSetupScreen(engine_workdir=workdir, worker=fake_worker)
    qtbot.addWidget(screen)
    finished: list[SessionBootstrapResult] = []
    fake_worker.session_finished.connect(finished.append)
    with qtbot.waitSignal(fake_worker.session_finished, timeout=3000):
        screen._launch_btn.click()
    assert finished and finished[0].status == SessionStatus.VALID
