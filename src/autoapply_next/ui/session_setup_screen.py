"""SessionSetupScreen: one-time Seek session bootstrap.

Reads the engine workdir's `sessions/seek_chrome_profile/` to detect whether a
session exists. The "Open Seek to log in" button launches a Playwright
Chromium with that user-data-dir so the user can log in manually (including
OTP). Closing the browser persists cookies for the engine to use.

The skeleton version just reports status. The slice-2 version wires the
actual browser launch.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class SessionSetupScreen(QWidget):
    def __init__(self, *, engine_workdir: Path):
        super().__init__()
        self._engine_workdir = engine_workdir

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Seek session")
        title.setFont(_h1())
        layout.addWidget(title)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        row = QHBoxLayout()
        self._open_btn = QPushButton("Open Seek to log in")
        self._open_btn.setStyleSheet(_primary_btn())
        self._open_btn.clicked.connect(self._on_open_clicked)
        row.addWidget(self._open_btn)
        self._refresh_btn = QPushButton("Re-check session")
        self._refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self._refresh_btn)
        row.addStretch(1)
        layout.addLayout(row)

        layout.addStretch(1)
        self._refresh()

    @Slot()
    def _refresh(self) -> None:
        profile = self._engine_workdir / "sessions" / "seek_chrome_profile"
        if profile.exists() and any(profile.iterdir()):
            self._status_label.setText(
                f"Seek session detected at:\n{profile}\n\nThe engine will "
                "reuse this on next run. If it is stale (the engine raises "
                "PermissionError on apply), click Open Seek to refresh."
            )
            self._status_label.setStyleSheet("color: #15803d;")
        else:
            self._status_label.setText(
                f"No Seek session at:\n{profile}\n\nClick Open Seek to log in. "
                "A Chromium window will open; complete the login (including OTP) "
                "then close the window. Cookies persist automatically."
            )
            self._status_label.setStyleSheet("color: #b91c1c;")

    @Slot()
    def _on_open_clicked(self) -> None:
        # Slice 2: wire a Playwright launch here. For the skeleton we surface a
        # note so the user knows what to do until the launch is implemented.
        self._status_label.setText(
            "Browser launch is wired in slice 2. For now, run "
            "`python -m setup_sessions` against the engine workdir from a "
            "terminal."
        )
        self._status_label.setStyleSheet("color: #c2410c;")


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f


def _primary_btn() -> str:
    return (
        "QPushButton { background: #1d4ed8; color: white; padding: 8px 16px; "
        "border-radius: 6px; }"
    )
