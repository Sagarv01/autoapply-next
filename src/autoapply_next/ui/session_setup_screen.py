"""SessionSetupScreen: one-time Seek account connection, in-app.

Replaces the old "run the CLI script" instruction with a button that drives
the engine worker. While the connection is running, the user logs into the
browser directly; we stream status from the worker via the `log` signal and
finalize via `session_finished`.

The tester build shows the raw status log + cookie-store internals for
diagnosis. The end-user build hides all of that and shows one plain
instruction plus a Connected / Not connected status.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..audience import Audience, current_audience
from ..engine.session_bootstrap import (
    SessionBootstrapResult,
    SessionStatus,
)
from ..engine.worker import EngineWorker

logger = logging.getLogger(__name__)


class SessionSetupScreen(QWidget):
    def __init__(
        self,
        *,
        engine_workdir: Path,
        worker: EngineWorker,
        audience: Audience | None = None,
    ):
        super().__init__()
        self._engine_workdir = engine_workdir
        self._worker = worker
        self._audience = audience or current_audience()
        self._user = self._audience is Audience.USER

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Seek account" if self._user else "Seek session")
        title.setFont(_h1())
        layout.addWidget(title)

        if self._user:
            intro = QLabel(
                "Connect your Seek account once so AutoApply can apply for you. "
                "A Seek window will open. Sign in the way you normally do (a code "
                "by text is fine), then close the window."
            )
            intro.setWordWrap(True)
            intro.setStyleSheet("color: #4b5563;")
            layout.addWidget(intro)

        layout.addWidget(self._build_status_block())
        layout.addWidget(self._build_button_row())
        layout.addWidget(self._build_progress_bar())

        # The raw status log is a tester diagnostic; end users never see it.
        log = self._build_log_view()
        if self._user:
            log.hide()
        else:
            layout.addWidget(log, stretch=1)
        if self._user:
            layout.addStretch(1)

        # Wire worker signals (only the ones this screen cares about).
        self._worker.log.connect(self._on_log)
        self._worker.session_finished.connect(self._on_session_finished)
        self._worker.state_changed.connect(self._on_worker_state)
        self._worker.failed.connect(self._on_worker_failed)

        self._refresh_local_status()

    # ---------------------------------------------------------- widgets

    def _build_status_block(self) -> QWidget:
        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(
            "padding: 10px; border: 1px solid #d1d5db; border-radius: 8px;"
        )
        return self._status_label

    def _build_button_row(self) -> QWidget:
        wrap = QFrame()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)

        self._launch_btn = QPushButton(
            "Connect your Seek account" if self._user else "Open Seek to log in"
        )
        self._launch_btn.setProperty("buttonRole", "primary")
        self._launch_btn.clicked.connect(self._on_launch_clicked)
        row.addWidget(self._launch_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        row.addWidget(self._cancel_btn)

        self._recheck_btn = QPushButton(
            "Check connection" if self._user else "Re-check existing session"
        )
        self._recheck_btn.clicked.connect(self._refresh_local_status)
        row.addWidget(self._recheck_btn)

        row.addStretch(1)
        return wrap

    def _build_progress_bar(self) -> QWidget:
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setVisible(False)
        return self._progress_bar

    def _build_log_view(self) -> QWidget:
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(500)
        self._log_view.setStyleSheet(
            "QPlainTextEdit { background: #0f172a; color: #d1d5db; "
            "font-family: 'SF Mono', 'Cascadia Code', Consolas, monospace; "
            "font-size: 12px; }"
        )
        self._log_view.setPlaceholderText(
            "Status updates from the session bootstrap will appear here."
        )
        return self._log_view

    # ---------------------------------------------------------- behaviour

    def _refresh_local_status(self) -> None:
        """Read the local profile to give a fast offline status."""
        profile = self._engine_workdir / "sessions" / "seek_chrome_profile"
        connected = profile.exists() and any(profile.iterdir())
        if self._user:
            if connected:
                self._set_status(
                    "valid_local",
                    "Connected. AutoApply can apply to jobs using your Seek account.",
                )
            else:
                self._set_status(
                    "missing",
                    "Not connected yet. Select Connect your Seek account, sign in "
                    "to Seek in the window that opens, then close it.",
                )
            return
        if connected:
            self._set_status(
                "valid_local",
                f"Seek user-data-dir present at:\n{profile}\n\n"
                "This is the cookie store the engine reuses. If apply later "
                "raises PermissionError, the cookies have expired; click "
                "Open Seek to log in.",
            )
        else:
            self._set_status(
                "missing",
                f"No Seek session at:\n{profile}\n\n"
                "Click Open Seek to launch a Chromium window. Sign in to "
                "Seek manually (including OTP). Close the browser when done; "
                "the app will verify the session.",
            )

    def _set_status(self, kind: str, text: str) -> None:
        self._status_label.setText(text)
        if kind == "valid" or kind == "valid_local":
            self._status_label.setStyleSheet(
                "padding: 10px; border: 1px solid #15803d; "
                "border-radius: 8px; background: #f0fdf4; color: #14532d;"
            )
        elif kind == "invalid":
            self._status_label.setStyleSheet(
                "padding: 10px; border: 1px solid #b91c1c; "
                "border-radius: 8px; background: #fef2f2; color: #7f1d1d;"
            )
        elif kind == "abandoned" or kind == "cancelled":
            self._status_label.setStyleSheet(
                "padding: 10px; border: 1px solid #c2410c; "
                "border-radius: 8px; background: #fff7ed; color: #7c2d12;"
            )
        elif kind == "error":
            self._status_label.setStyleSheet(
                "padding: 10px; border: 1px solid #b91c1c; "
                "border-radius: 8px; background: #fef2f2; color: #7f1d1d;"
            )
        else:
            self._status_label.setStyleSheet(
                "padding: 10px; border: 1px solid #d1d5db; border-radius: 8px;"
            )

    @Slot()
    def _on_launch_clicked(self) -> None:
        self._log_view.clear()
        self._worker.launch_session_browser()

    @Slot()
    def _on_cancel_clicked(self) -> None:
        self._worker.cancel()

    @Slot(str)
    def _on_log(self, line: str) -> None:
        # Tester diagnostic only; the log view is hidden in the user build.
        self._log_view.appendPlainText(line)

    @Slot(object)
    def _on_session_finished(self, result: SessionBootstrapResult) -> None:
        self._log_view.appendPlainText(f"[result] {result.status.value}: {result.message}")
        if self._user:
            plain = {
                SessionStatus.VALID: ("valid", "Connected. You're all set."),
                SessionStatus.INVALID: (
                    "invalid",
                    "That didn't connect. Please try again.",
                ),
                SessionStatus.ABANDONED: (
                    "abandoned",
                    "Sign-in wasn't finished. Try again when you're ready.",
                ),
                SessionStatus.CANCELLED: ("cancelled", "Connection cancelled."),
                SessionStatus.ERROR: (
                    "error",
                    "Something went wrong connecting. Please try again.",
                ),
            }.get(result.status)
            if plain is not None:
                self._set_status(*plain)
            return
        if result.status == SessionStatus.VALID:
            self._set_status("valid", result.message)
        elif result.status == SessionStatus.INVALID:
            self._set_status("invalid", result.message)
        elif result.status == SessionStatus.ABANDONED:
            self._set_status("abandoned", result.message)
        elif result.status == SessionStatus.CANCELLED:
            self._set_status("cancelled", result.message)
        elif result.status == SessionStatus.ERROR:
            self._set_status(
                "error",
                result.message + (
                    f"\n\nDetails: {result.error_message}"
                    if result.error_message else ""
                ),
            )

    @Slot(str)
    def _on_worker_state(self, state: str) -> None:
        running = state in ("running", "cancelling")
        self._launch_btn.setEnabled(not running)
        self._cancel_btn.setEnabled(running)
        self._progress_bar.setVisible(running)

    @Slot(str, str)
    def _on_worker_failed(self, op: str, msg: str) -> None:
        if op == "session":
            if self._user:
                self._set_status(
                    "error", "Something went wrong connecting. Please try again."
                )
            else:
                self._set_status("error", msg)


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f
