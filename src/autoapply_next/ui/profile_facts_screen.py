"""ProfileFactsScreen: the 19-question screening profile (Phase B).

Built dynamically from CANDIDATE_FIELDS so the form and the completion gate never
drift. Bool facts use a Select / Yes / No combo (unanswered = None, so the gate
sees it as blank); choice facts preselect their default (EEO -> prefer_not_to_say);
the visa fields appear only when the user is not a citizen/PR. SAVE runs off the
GUI thread via the injected AsyncTaskRunner (disk write must not freeze the wizard).

VOICE: field labels come from the model; the few strings here are plain drafts.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..onboarding import profile_facts
from ..onboarding.profile_facts import CANDIDATE_FIELDS

logger = logging.getLogger(__name__)

_SAVE_TOKEN = "profile_save"


class ProfileFactsScreen(QWidget):
    """Capture + save the candidate screening facts. Emits `saved` on success."""

    saved = Signal()

    def __init__(self, *, engine_workdir, runner):
        super().__init__()
        self._engine_workdir = Path(engine_workdir)
        self._runner = runner
        self._runner.succeeded.connect(self._on_succeeded)
        self._runner.failed.connect(self._on_failed)
        self._rows: dict[str, dict] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        title = QLabel("About you")
        title.setFont(_h1())
        outer.addWidget(title)
        intro = QLabel(
            "These let AutoApply answer employer screening questions truthfully on "
            "your behalf. Your answers are saved only on this computer."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #6b7280;")
        outer.addWidget(intro)

        # Scroll: 19 fields is taller than most windows.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_host = QWidget()
        self._form = QFormLayout(form_host)
        self._form.setLabelAlignment(Qt.AlignLeft)
        for f in CANDIDATE_FIELDS:
            self._add_field(f)
        scroll.setWidget(form_host)
        outer.addWidget(scroll, stretch=1)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #b91c1c;")
        self._error.setVisible(False)
        outer.addWidget(self._error)

        self._save_btn = QPushButton("Save and continue")
        self._save_btn.setProperty("buttonRole", "primary")
        self._save_btn.clicked.connect(self.submit_save)
        outer.addWidget(self._save_btn)

        self._load()
        self._update_conditionals()

    # ------------------------------------------------------- field building
    def _add_field(self, f) -> None:
        label = QLabel(f.label)
        if f.kind == "bool":
            w = QComboBox()
            w.addItem("Select...", None)
            w.addItem("Yes", True)
            w.addItem("No", False)
            get = w.currentData
            set_ = lambda v, _w=w: _w.setCurrentIndex(_index_for_data(_w, v))
            if f.key == "is_citizen":
                w.currentIndexChanged.connect(self._update_conditionals)
        elif f.kind == "choice":
            w = QComboBox()
            for c in f.choices:
                w.addItem(_humanize(c), c)
            get = w.currentData
            set_ = lambda v, _w=w: _w.setCurrentIndex(_index_for_data(_w, v))
        else:  # text / int
            w = QLineEdit()
            if f.help_text:
                w.setPlaceholderText(f.help_text)
            get = lambda _w=w: (_w.text().strip() or None)
            set_ = lambda v, _w=w: _w.setText("" if v is None else str(v))
        self._form.addRow(label, w)
        self._rows[f.key] = {"field": f, "widget": w, "label": label, "get": get, "set": set_, "visible": True}

    # ------------------------------------------------------- public/test API
    def has_field(self, key: str) -> bool:
        return key in self._rows

    def is_field_visible(self, key: str) -> bool:
        return self._rows[key]["visible"]

    def set_field(self, key: str, value) -> None:
        self._rows[key]["set"](value)

    def collect_values(self) -> dict:
        """Read every field on the GUI thread into a plain dict (call before any
        off-thread save)."""
        return {k: row["get"]() for k, row in self._rows.items()}

    def error_text(self) -> str:
        return self._error.text()

    def submit_save(self) -> None:
        values = self.collect_values()
        missing = profile_facts.missing_required(values)
        if missing:
            labels = ", ".join(self._rows[k]["field"].label for k in missing)
            self._show_error(f"Please answer: {labels}")
            return
        self._clear_error()
        self._save_btn.setEnabled(False)
        wd = self._engine_workdir
        self._runner.submit(lambda: profile_facts.save_facts(wd, values), token=_SAVE_TOKEN)

    # ------------------------------------------------------- runner callbacks
    @Slot(object, object)
    def _on_succeeded(self, result, token) -> None:
        if token != _SAVE_TOKEN:
            return
        self._save_btn.setEnabled(True)
        self._clear_error()
        self.saved.emit()

    @Slot(str, object)
    def _on_failed(self, message, token) -> None:
        if token != _SAVE_TOKEN:
            return
        self._save_btn.setEnabled(True)
        self._show_error(f"We couldn't save your answers. {message}")

    # ------------------------------------------------------- internals
    def _load(self) -> None:
        existing = profile_facts.load_facts(self._engine_workdir)
        for key, value in existing.items():
            if value is not None and key in self._rows:
                self._rows[key]["set"](value)

    def _update_conditionals(self) -> None:
        # Visa fields are needed only when the user is NOT a citizen/PR.
        citizen = self._rows["is_citizen"]["get"]()
        show_visa = citizen is not True
        for key in ("visa_label", "visa_expiry"):
            self._set_row_visible(key, show_visa)

    def _set_row_visible(self, key: str, visible: bool) -> None:
        row = self._rows[key]
        row["visible"] = visible
        row["widget"].setVisible(visible)
        row["label"].setVisible(visible)

    def _show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.setVisible(True)

    def _clear_error(self) -> None:
        self._error.clear()
        self._error.setVisible(False)


def _index_for_data(combo: QComboBox, value) -> int:
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            return i
    return 0


def _humanize(choice: str) -> str:
    return choice.replace("_", " ").strip().capitalize()


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f
