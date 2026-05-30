"""RunScreen: drive one application end-to-end with live progress.

The walking skeleton (Phase 2) lives here: paste a Seek URL, click "Run
dry-run", watch the progress flow through PEEK -> SCORE -> TAILOR -> APPLY ->
DRY_RUN_VERIFIED, see the screenshot when it lands. This screen is also where
the iterate-fix loop runs.

In Phase 3 the Queue screen will populate the URL field automatically and the
user just clicks Run.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QSize, Slot
from PySide6.QtGui import QFont, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..engine.progress import ProgressEvent, ProgressStage
from ..engine.results import ApplicationResult, ApplicationStatus
from ..engine.worker import EngineWorker
from ..safe_ui import safe_slot, show_error_dialog
from .settings_store import SettingsStore

logger = logging.getLogger(__name__)


STAGE_ORDER = [
    ProgressStage.PEEK,
    ProgressStage.SCORE,
    ProgressStage.TAILOR,
    ProgressStage.APPLY,
    ProgressStage.DRY_RUN_VERIFIED,
]


class RunScreen(QWidget):
    def __init__(self, *, worker: EngineWorker, settings: SettingsStore):
        super().__init__()
        self._worker = worker
        self._settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Run")
        title.setFont(_h1())
        layout.addWidget(title)

        layout.addWidget(self._build_url_row())
        layout.addWidget(self._build_stage_row())
        layout.addWidget(self._build_log_block(), stretch=1)
        layout.addWidget(self._build_screenshot_block(), stretch=1)

        # Wire signals.
        self._worker.state_changed.connect(self._on_state)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)

        self._settings.allow_real_submit_changed.connect(self._refresh_button_label)
        self._refresh_button_label(self._settings.allow_real_submit)

    # ------------------------------------------------------------- widgets

    def set_url(self, url: str) -> None:
        """Public: called by MainWindow when the Queue requests a run."""
        if url:
            self._url_input.setText(url)
            self._settings.selected_job_url = url

    def _build_url_row(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("url-row")
        wrap.setStyleSheet(
            "QFrame#url-row { border: 1px solid #d1d5db; border-radius: 8px; padding: 8px; }"
        )
        h = QHBoxLayout(wrap)
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText(
            "Paste a Seek job URL, e.g. https://au.seek.com/job/91283052"
        )
        last = self._settings.selected_job_url
        if last:
            self._url_input.setText(last)
        h.addWidget(self._url_input, stretch=1)

        self._run_btn = QPushButton()
        self._run_btn.setMinimumWidth(180)
        self._run_btn.clicked.connect(self._on_run_clicked)
        h.addWidget(self._run_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        h.addWidget(self._cancel_btn)
        return wrap

    def _build_stage_row(self) -> QWidget:
        wrap = QFrame()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 4, 0, 4)
        self._stage_labels: dict[ProgressStage, QLabel] = {}
        for stage in STAGE_ORDER:
            lbl = QLabel(_stage_label(stage))
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setMinimumHeight(28)
            lbl.setStyleSheet(_stage_idle_css())
            h.addWidget(lbl, stretch=1)
            self._stage_labels[stage] = lbl
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setVisible(False)
        wrapper_v = QVBoxLayout()
        wrapper_v.addWidget(wrap)
        wrapper_v.addWidget(self._progress_bar)
        outer = QWidget()
        outer.setLayout(wrapper_v)
        return outer

    def _build_log_block(self) -> QWidget:
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        self._log_view.setStyleSheet(
            "QPlainTextEdit { background: #0f172a; color: #d1d5db; "
            "font-family: 'SF Mono', 'Cascadia Code', Consolas, monospace; "
            "font-size: 12px; }"
        )
        self._log_view.setPlaceholderText("Engine log will stream here.")
        return self._log_view

    def _build_screenshot_block(self) -> QWidget:
        wrap = QFrame()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        title = QLabel("Dry-run screenshot")
        title.setFont(_h2())
        header.addWidget(title)
        header.addStretch(1)
        self._screenshot_path_label = QLabel("(no screenshot yet)")
        self._screenshot_path_label.setStyleSheet("color: #6b7280;")
        header.addWidget(self._screenshot_path_label)
        v.addLayout(header)

        self._screenshot_label = QLabel()
        self._screenshot_label.setAlignment(Qt.AlignCenter)
        self._screenshot_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        self._screenshot_label.setMinimumHeight(220)
        self._screenshot_label.setStyleSheet(
            "QLabel { border: 1px dashed #d1d5db; background: #f9fafb; }"
        )
        scroll = QScrollArea()
        scroll.setWidget(self._screenshot_label)
        scroll.setWidgetResizable(True)
        v.addWidget(scroll)
        return wrap

    # ------------------------------------------------------------- handlers

    @Slot(bool)
    def _refresh_button_label(self, allowed: bool) -> None:
        if allowed:
            self._run_btn.setText("Run with LIVE submit")
            self._run_btn.setStyleSheet(
                "QPushButton { background: #b91c1c; color: white; "
                "padding: 8px 16px; border-radius: 6px; font-weight: bold; }"
            )
        else:
            self._run_btn.setText("Run dry-run")
            self._run_btn.setStyleSheet(
                "QPushButton { background: #15803d; color: white; "
                "padding: 8px 16px; border-radius: 6px; font-weight: bold; }"
            )

    @Slot()
    @safe_slot
    def _on_run_clicked(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            show_error_dialog(
                self,
                "URL needed",
                "Paste a Seek job URL into the field above before clicking Run dry-run.",
            )
            self._url_input.setFocus()
            return
        if "seek.com" not in url:
            show_error_dialog(
                self,
                "Seek only",
                "The minimal product supports Seek quick-apply only. "
                "Other boards are not implemented in this build.",
            )
            return

        if self._settings.allow_real_submit:
            confirmed = QMessageBox.question(
                self,
                "Real submission?",
                f"About to file a REAL application to:\n\n{url}\n\nProceed?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if confirmed != QMessageBox.Yes:
                return

        self._settings.selected_job_url = url
        self._reset_stage_styles()
        self._log_view.clear()
        self._screenshot_label.clear()
        self._screenshot_label.setText("(running...)")
        self._screenshot_path_label.setText("")

        self._worker.run_job(url, self._settings.allow_real_submit)

    @Slot()
    def _on_cancel_clicked(self) -> None:
        self._worker.cancel()

    @Slot(str)
    def _on_state(self, state: str) -> None:
        running = state in ("running", "cancelling")
        self._run_btn.setEnabled(not running)
        self._cancel_btn.setEnabled(running)
        self._progress_bar.setVisible(running)

    @Slot(object)
    def _on_progress(self, ev: ProgressEvent) -> None:
        self._append_log(f"[{ev.stage.value}] {ev.message}")
        # Mark the stage as active in the strip.
        active = _resolve_active_stage(ev.stage)
        if active is not None:
            for stage, lbl in self._stage_labels.items():
                if stage == active:
                    lbl.setStyleSheet(_stage_active_css(stage))
                elif _stage_index(stage) < _stage_index(active):
                    lbl.setStyleSheet(_stage_done_css())

    @Slot(object)
    @safe_slot
    def _on_finished(self, result: ApplicationResult) -> None:
        self._append_log(f"[finished] {result.status.value}")
        if result.status == ApplicationStatus.DRY_RUN_VERIFIED:
            self._show_screenshot(result.dry_run_screenshot)
            self._stage_labels[ProgressStage.DRY_RUN_VERIFIED].setStyleSheet(
                _stage_done_css()
            )
        elif result.status == ApplicationStatus.SUBMITTED:
            self._screenshot_label.setPixmap(QPixmap())
            self._screenshot_label.setText("Submitted (real submission, no screenshot).")
            self._stage_labels[ProgressStage.DRY_RUN_VERIFIED].setStyleSheet(
                _stage_done_css()
            )
            self._stage_labels[ProgressStage.DRY_RUN_VERIFIED].setText("Submitted")
        elif result.status == ApplicationStatus.SKIPPED_LOW_SCORE:
            self._append_log(
                f"Skipped: score {result.score} below threshold"
            )
            self._screenshot_label.setPixmap(QPixmap())
            self._screenshot_label.setText(
                f"Skipped: match score {result.score} is below the threshold "
                f"(set in Settings).\nThe engine did not tailor or apply this job."
            )
            self._screenshot_label.setStyleSheet(
                "QLabel { border: 1px solid #c2410c; background: #fff7ed; "
                "color: #7c2d12; padding: 12px; }"
            )
        elif result.status == ApplicationStatus.CANCELLED:
            self._append_log("Cancelled by user.")
            self._screenshot_label.setPixmap(QPixmap())
            self._screenshot_label.setText(
                "Cancelled. The browser context was torn down."
            )
            self._screenshot_label.setStyleSheet(
                "QLabel { border: 1px solid #6b7280; background: #f9fafb; "
                "color: #374151; padding: 12px; }"
            )
        elif result.status == ApplicationStatus.FAILED:
            err_line = (
                f"FAILED ({result.exception_type}): {result.error_message}"
            )
            self._append_log(err_line)
            # Mark the strip's active stage red so the visible state matches
            # the log. The adapter records which stage failed in detail.
            self._mark_failed_stage()
            # Replace the "(running...)" placeholder with a red error panel.
            self._screenshot_label.setPixmap(QPixmap())
            self._screenshot_label.setText(self._friendly_failure_text(result))
            self._screenshot_label.setStyleSheet(
                "QLabel { border: 2px solid #b91c1c; background: #fef2f2; "
                "color: #7f1d1d; padding: 12px; }"
            )
            # And a non-blocking dialog so the user definitely notices.
            show_error_dialog(
                self,
                "Dry-run failed",
                self._friendly_failure_text(result),
                err_line,
            )

    @Slot(str, str)
    def _on_failed(self, job_url: str, message: str) -> None:
        self._append_log(f"[worker-failed] {job_url}: {message}")

    # ------------------------------------------------------------- helpers

    def _reset_stage_styles(self) -> None:
        for lbl in self._stage_labels.values():
            lbl.setStyleSheet(_stage_idle_css())

    def _append_log(self, line: str) -> None:
        self._log_view.appendPlainText(line)
        # Auto-scroll. In PySide6 the enum members are accessed via the class
        # (not the instance), unlike PyQt5/6 where `cursor.End` worked.
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._log_view.setTextCursor(cursor)

    def _mark_failed_stage(self) -> None:
        """Find the last stage we styled as 'active' and recolour it red."""
        for stage, lbl in self._stage_labels.items():
            css = lbl.styleSheet()
            if "#1d4ed8" in css or "#15803d" in css and "background" in css:
                lbl.setStyleSheet(
                    "padding: 6px; border: 1px solid #b91c1c; "
                    "border-radius: 6px; color: white; background: #b91c1c; "
                    "font-weight: bold;"
                )

    def _friendly_failure_text(self, result: ApplicationResult) -> str:
        et = result.exception_type or "Error"
        msg = (result.error_message or "(no message)").strip()
        # Bespoke friendly hints for known engine error types. Anything not
        # listed falls through with the raw message.
        if et == "JobNotQuickApplyError":
            return (
                "This job is not a Seek quick-apply listing (it routes to an "
                "external recruiter site). Pick a different job from the "
                "Queue, or apply manually on the company's site."
            )
        if et == "PermissionError":
            return (
                "Seek says we are not signed in. Open the Seek session screen "
                "and click 'Open Seek to log in' to refresh the session."
            )
        if et == "BoardBlockedError":
            return (
                "Seek blocked the request (captcha, rate-limit, or session "
                "issue). Wait a minute, then retry. If it keeps blocking, "
                "log in again from the Seek session screen."
            )
        if et == "TimeoutError":
            return (
                "The apply form took too long to complete. Try again. If it "
                "fails repeatedly the form may have changed; check the log."
            )
        if et == "CoverLetterQualityError":
            return (
                "The tailored cover letter did not pass the engine's quality "
                "gate. Rerun, or edit your profile and try again."
            )
        return f"Dry-run failed at {result.exception_type}: {msg}"

    def _show_screenshot(self, path: Path | None) -> None:
        if path is None or not Path(path).exists():
            self._screenshot_label.setText("(no screenshot)")
            self._screenshot_path_label.setText("")
            return
        pix = QPixmap(str(path))
        if pix.isNull():
            self._screenshot_label.setText(f"(failed to load screenshot at {path})")
            self._screenshot_path_label.setText(str(path))
            return
        scaled = pix.scaled(
            QSize(900, 600), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self._screenshot_label.setPixmap(scaled)
        self._screenshot_path_label.setText(str(path))


# ----------------------------------------------------------------------------- styles


def _stage_label(stage: ProgressStage) -> str:
    return {
        ProgressStage.PEEK: "Peek",
        ProgressStage.SCORE: "Score",
        ProgressStage.TAILOR: "Tailor",
        ProgressStage.APPLY: "Apply",
        ProgressStage.DRY_RUN_VERIFIED: "Dry-run OK",
    }[stage]


def _stage_index(stage: ProgressStage) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return -1


def _resolve_active_stage(stage: ProgressStage) -> ProgressStage | None:
    if stage in STAGE_ORDER:
        return stage
    if stage in (ProgressStage.FAILED, ProgressStage.CANCELLED, ProgressStage.SUBMITTED):
        return None
    return None


def _stage_idle_css() -> str:
    return (
        "padding: 6px; border: 1px solid #d1d5db; border-radius: 6px; "
        "color: #6b7280; background: #f9fafb;"
    )


def _stage_active_css(stage: ProgressStage) -> str:
    base = (
        "padding: 6px; border: 1px solid #1d4ed8; border-radius: 6px; "
        "color: white; font-weight: bold;"
    )
    color = (
        "#1d4ed8"
        if stage != ProgressStage.DRY_RUN_VERIFIED
        else "#15803d"
    )
    return f"{base} background: {color};"


def _stage_done_css() -> str:
    return (
        "padding: 6px; border: 1px solid #15803d; border-radius: 6px; "
        "color: white; background: #15803d;"
    )


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f


def _h2() -> QFont:
    f = QFont()
    f.setPointSize(13)
    f.setBold(True)
    return f
