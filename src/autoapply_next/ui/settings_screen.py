"""Settings: tester-only submission gate + thresholds."""

from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..audience import Audience, current_audience
from .settings_store import SettingsStore


class SettingsScreen(QWidget):
    def __init__(self, *, settings: SettingsStore, audience: Audience | None = None):
        super().__init__()
        self._settings = settings
        self._audience = audience or current_audience()
        if self._audience is Audience.USER and not self._settings.pace_between_applies:
            self._settings.pace_between_applies = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("Settings")
        title.setFont(_h1())
        layout.addWidget(title)

        if self._audience is Audience.TESTER:
            layout.addWidget(self._build_safety_block())
        layout.addWidget(self._build_thresholds_block())
        layout.addStretch(1)

    # ----------------------------------------------------------- safety block

    def _build_safety_block(self) -> QWidget:
        block = QFrame()
        block.setObjectName("safety-block")
        block.setStyleSheet(
            "QFrame#safety-block { border: 2px solid #b91c1c; "
            "border-radius: 8px; padding: 12px; background: #fef2f2; }"
        )
        v = QVBoxLayout(block)
        v.setContentsMargins(16, 16, 16, 16)

        heading = QLabel("Real submission gate")
        heading.setFont(_h2())
        heading.setStyleSheet("color: #b91c1c;")
        v.addWidget(heading)

        warning = QLabel(
            "When ON, the engine submits real applications to real employers under your name. "
            "This setting starts OFF. Turning it on requires confirmation and the change is "
            "logged. Even with this OFF, the engine drives all the way to the submit-ready "
            "screen so you can see what would be submitted; it just does not click."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #374151;")
        v.addWidget(warning)

        self._allow_box = QCheckBox(
            "I understand. Allow real submission (ALLOW_REAL_SUBMIT=true)."
        )
        self._allow_box.setChecked(self._settings.allow_real_submit)
        self._allow_box.toggled.connect(self._on_allow_toggled)
        v.addWidget(self._allow_box)

        row = QHBoxLayout()
        row.addStretch(1)
        self._state_label = QLabel()
        self._state_label.setMinimumWidth(160)
        self._state_label.setAlignment(Qt.AlignCenter)
        row.addWidget(self._state_label)
        v.addLayout(row)
        self._refresh_state_label(self._settings.allow_real_submit)

        return block

    @Slot(bool)
    def _on_allow_toggled(self, checked: bool) -> None:
        if checked:
            confirmed = QMessageBox.question(
                self,
                "Enable real submission?",
                (
                    "This allows the engine to submit applications under your real name. "
                    "Each job submitted this way is a real application that the employer "
                    "will see. Are you certain?"
                ),
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if confirmed != QMessageBox.Yes:
                # Roll back the check.
                self._allow_box.blockSignals(True)
                self._allow_box.setChecked(False)
                self._allow_box.blockSignals(False)
                return
        self._settings.allow_real_submit = checked
        self._refresh_state_label(checked)

    def _refresh_state_label(self, allowed: bool) -> None:
        if allowed:
            self._state_label.setText("LIVE SUBMIT enabled")
            self._state_label.setStyleSheet(
                "padding: 4px 12px; background: #b91c1c; color: white; "
                "border-radius: 4px; font-weight: bold;"
            )
        else:
            self._state_label.setText("Dry-run only")
            self._state_label.setStyleSheet(
                "padding: 4px 12px; background: #15803d; color: white; "
                "border-radius: 4px; font-weight: bold;"
            )

    # ------------------------------------------------------- thresholds block

    def _build_thresholds_block(self) -> QWidget:
        block = QFrame()
        block.setObjectName("thresholds-block")
        block.setStyleSheet(
            "QFrame#thresholds-block { border: 1px solid #d1d5db; "
            "border-radius: 8px; padding: 12px; }"
        )
        form = QFormLayout(block)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(8)

        self._threshold_spin = QSpinBox()
        self._threshold_spin.setRange(0, 100)
        self._threshold_spin.setValue(self._settings.match_threshold)
        self._threshold_spin.setSuffix(" / 100")
        self._threshold_spin.setSingleStep(5)
        self._threshold_spin.valueChanged.connect(
            lambda v: setattr(self._settings, "match_threshold", v)
        )
        form.addRow(
            "Minimum match score (higher = pickier):"
            if self._audience is Audience.USER
            else "Match score threshold:",
            self._threshold_spin,
        )

        # No daily application cap in the user build. The tester build keeps a
        # configurable cap for diagnosis.
        if self._audience is not Audience.USER:
            self._cap_spin = QSpinBox()
            self._cap_spin.setRange(0, 999)
            self._cap_spin.setValue(self._settings.daily_cap)
            self._cap_spin.setSuffix(" / day")
            self._cap_spin.setToolTip(
                "Maximum real submissions per day. AutoApply also has a hard 100/day ceiling."
            )
            self._cap_spin.valueChanged.connect(
                lambda v: setattr(self._settings, "daily_cap", v)
            )
            form.addRow("Daily application cap:", self._cap_spin)

        self._pace_box = QCheckBox("Pace between applies (60-120s, recommended)")
        self._pace_box.setChecked(self._settings.pace_between_applies)
        self._pace_box.setToolTip(
            "On (default): the worker waits 60-120s between live "
            "submissions, mirroring job-finder's anti-bot pacing. "
            "Off: applies run back-to-back with no pause. Faster, but "
            "Seek is more likely to flag the account."
        )
        self._pace_box.toggled.connect(
            lambda v: setattr(self._settings, "pace_between_applies", v)
        )
        self._pace_label = QLabel("Throttle:")
        form.addRow(self._pace_label, self._pace_box)
        if self._audience is Audience.USER:
            self._pace_label.hide()
            self._pace_box.hide()

        note = QLabel(
            "The minimum match decides which jobs AutoApply applies to. "
            "Higher means stricter."
            if self._audience is Audience.USER
            else (
                "Threshold determines when a scraped job would be skipped at apply "
                "time. Daily cap limits live submissions across a day. Throttle off "
                "means dry-run applies can run back-to-back; live submissions still "
                "keep the 60s safety floor."
            )
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #6b7280; font-size: 12px;")
        form.addRow("", note)
        return block


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f


def _h2() -> QFont:
    f = QFont()
    f.setPointSize(14)
    f.setBold(True)
    return f
