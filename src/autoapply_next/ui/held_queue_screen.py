"""HeldQueueScreen: "Waiting on you" — answer the screening questions the bot
couldn't answer from your facts.

Shows one pending question at a time with a text field (or a choice for
single-select). Answering runs screening.answer_flow.answer_and_unblock OFF the
GUI thread: it remembers the answer for future jobs (the bank) and re-queues
every parked job that was waiting on it. Then it advances to the next pending
question. The questions_changed signal carries the pending count so MainWindow
can show a badge.

VOICE: the strings are plain drafts for the humanize pass (WAITING_ON_YOU_PROMPT
comes from run_status).
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..screening.answer_flow import answer_and_unblock
from ..screening.held_queue import HeldQueue
from .run_status import WAITING_ON_YOU_PROMPT

logger = logging.getLogger(__name__)

_ANSWER_TOKEN = "held_answer"


class HeldQueueScreen(QWidget):
    answered = Signal()           # a question was answered + persisted
    questions_changed = Signal(int)  # current pending count (for a badge)

    def __init__(self, *, engine_workdir, runner):
        super().__init__()
        self._engine_workdir = Path(engine_workdir)
        self._runner = runner
        self._runner.succeeded.connect(self._on_succeeded)
        self._runner.failed.connect(self._on_failed)
        self._current = None
        self._pending_count = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 36, 36, 36)
        layout.setSpacing(12)

        title = QLabel("Waiting on you")
        title.setFont(_h1())
        layout.addWidget(title)
        self._intro = QLabel(WAITING_ON_YOU_PROMPT)
        self._intro.setWordWrap(True)
        self._intro.setStyleSheet("color: #6b7280;")
        layout.addWidget(self._intro)

        self._question = QLabel("")
        self._question.setWordWrap(True)
        self._question.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(self._question)

        self._line = QLineEdit()
        self._line.setPlaceholderText("Your answer")
        layout.addWidget(self._line)
        self._combo = QComboBox()
        layout.addWidget(self._combo)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._save_btn = QPushButton("Save answer")
        self._save_btn.setProperty("buttonRole", "primary")
        self._save_btn.clicked.connect(self.submit_answer)
        layout.addWidget(self._save_btn)
        layout.addStretch(1)

        self.refresh()

    # -------------------------------------------------------- public/test API
    def refresh(self) -> None:
        pending = HeldQueue.load(self._held_path()).pending()
        self._pending_count = len(pending)
        self.questions_changed.emit(self._pending_count)
        if not pending:
            self._current = None
            self._question.setText("")
            self._show_inputs(False)
            self._status.setText("You're all caught up. Nothing needs your answer.")
            return
        self._current = pending[0]
        self._question.setText(self._current.question)
        if self._current.options:
            self._combo.clear()
            self._combo.addItems(list(self._current.options))
            self._show_inputs(True, choice=True)
        else:
            self._line.clear()
            self._show_inputs(True, choice=False)
        remaining = f" ({self._pending_count} to answer)" if self._pending_count > 1 else ""
        self._status.setText(f"Answer this and AutoApply will pick the job back up.{remaining}")

    def pending_count(self) -> int:
        return self._pending_count

    def current_question(self) -> str:
        return self._question.text()

    def current_options(self) -> list[str]:
        return [self._combo.itemText(i) for i in range(self._combo.count())]

    def set_answer(self, text: str) -> None:
        if self._current is not None and self._current.options:
            idx = self._combo.findText(text)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
        else:
            self._line.setText(text)

    def status_text(self) -> str:
        return self._status.text()

    def submit_answer(self) -> None:
        if self._current is None:
            return
        answer = self._read_answer()
        if not answer:
            self._status.setText("Please type an answer before saving.")
            return
        question = self._current.question
        wd = self._engine_workdir
        self._save_btn.setEnabled(False)
        self._runner.submit(
            lambda: answer_and_unblock(wd, question, answer), token=_ANSWER_TOKEN
        )

    # -------------------------------------------------------- callbacks
    @Slot(object, object)
    def _on_succeeded(self, result, token) -> None:
        if token != _ANSWER_TOKEN:
            return
        self._save_btn.setEnabled(True)
        requeued = (result or {}).get("requeued", 0)
        self.answered.emit()
        self.refresh()
        if requeued:
            jobs = "job" if requeued == 1 else "jobs"
            self._status.setText(
                f"Saved. {requeued} {jobs} can continue now, and AutoApply will "
                "reuse this answer next time."
            )

    @Slot(str, object)
    def _on_failed(self, message, token) -> None:
        if token != _ANSWER_TOKEN:
            return
        self._save_btn.setEnabled(True)
        self._status.setText(f"We couldn't save that. {message}")

    # -------------------------------------------------------- internals
    def _held_path(self) -> Path:
        return self._engine_workdir / "held_questions.json"

    def _read_answer(self) -> str:
        if self._current is not None and self._current.options:
            return self._combo.currentText().strip()
        return self._line.text().strip()

    def _show_inputs(self, visible: bool, *, choice: bool = False) -> None:
        self._line.setVisible(visible and not choice)
        self._combo.setVisible(visible and choice)
        self._save_btn.setVisible(visible)


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f
