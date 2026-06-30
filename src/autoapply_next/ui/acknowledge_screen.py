"""AcknowledgeScreen: the onboarding honesty line.

The user ticks the honesty line (every application goes out under their name)
before the bot unlocks. The acknowledgement flag is persisted off the GUI thread
via the runner; the screen emits `acknowledged` once saved.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget

from ..onboarding import state as ob
from .run_status import HONESTY_LINE

logger = logging.getLogger(__name__)

_ACK_TOKEN = "acknowledge"


class AcknowledgeScreen(QWidget):
    acknowledged = Signal()

    def __init__(self, *, engine_workdir, runner):
        super().__init__()
        self._engine_workdir = Path(engine_workdir)
        self._runner = runner
        self._runner.succeeded.connect(self._on_succeeded)
        self._runner.failed.connect(self._on_failed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 48, 48, 48)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(18)

        title = QLabel("One last thing")
        title.setFont(_h1())
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self._check = QCheckBox(HONESTY_LINE)
        self._check.setStyleSheet("font-size: 14px;")
        self._check.toggled.connect(self._on_toggled)
        layout.addWidget(self._check)

        self._error = QLabel("")
        self._error.setStyleSheet("color: #b91c1c;")
        self._error.setVisible(False)
        self._error.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._error)

        self._continue = QPushButton("I understand, continue")
        self._continue.setProperty("buttonRole", "primary")
        self._continue.setEnabled(False)
        self._continue.clicked.connect(self.submit)
        layout.addWidget(self._continue, alignment=Qt.AlignCenter)

    # -------------------------------------------------------- public/test API
    def honesty_text(self) -> str:
        return self._check.text()

    def can_continue(self) -> bool:
        return self._continue.isEnabled()

    def set_checked(self, value: bool) -> None:
        self._check.setChecked(value)

    def submit(self) -> None:
        if not self._check.isChecked():
            return
        self._continue.setEnabled(False)
        wd = self._engine_workdir
        self._runner.submit(lambda: ob.set_acknowledged(wd, True), token=_ACK_TOKEN)

    # -------------------------------------------------------- callbacks
    def _on_toggled(self, checked: bool) -> None:
        self._continue.setEnabled(checked)

    @Slot(object, object)
    def _on_succeeded(self, result, token) -> None:
        if token != _ACK_TOKEN:
            return
        self.acknowledged.emit()

    @Slot(str, object)
    def _on_failed(self, message, token) -> None:
        if token != _ACK_TOKEN:
            return
        self._continue.setEnabled(True)
        self._error.setText(f"We couldn't save that. {message}")
        self._error.setVisible(True)


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f
