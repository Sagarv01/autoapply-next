"""RunStatusWidget: a one-line status display for the running bot.

Shows status_line(state) for normal RunStates, and during the pacing wait between
submits it rotates the three reassuring cooldown messages on a timer so a long
60-180s gap never reads as a frozen app. The batch view drives it: set_state on
progress/finish, start_cooldown when the engine signals a pacing wait.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import run_status
from .run_status import RunState

# How often the cooldown message rotates (ms). The pacing wait is 60-180s, so a
# few rotations reassure the user it's working.
_ROTATE_MS = 6000


class RunStatusWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._cooldown = False
        self._cooldown_idx = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel(run_status.status_line(RunState.IDLE))
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet("color: #374151; font-size: 13px;")
        layout.addWidget(self._label)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # -------------------------------------------------------- public/test API
    def set_state(self, state: RunState, *, done: int | None = None, total: int | None = None) -> None:
        self._stop_cooldown_timer()
        self._label.setText(run_status.status_line(state, done=done, total=total))

    def start_cooldown(self, seconds: float | None = None) -> None:
        self._cooldown = True
        self._cooldown_idx = 0
        self._label.setText(run_status.cooldown_message(0))
        self._timer.start(_ROTATE_MS)
        if seconds:
            QTimer.singleShot(int(seconds * 1000), self.stop_cooldown)

    def stop_cooldown(self) -> None:
        self._stop_cooldown_timer()

    def is_cooling_down(self) -> bool:
        return self._cooldown

    def text(self) -> str:
        return self._label.text()

    # -------------------------------------------------------- internals
    def _tick(self) -> None:
        self._cooldown_idx += 1
        self._label.setText(run_status.cooldown_message(self._cooldown_idx))

    def _stop_cooldown_timer(self) -> None:
        self._cooldown = False
        self._timer.stop()
