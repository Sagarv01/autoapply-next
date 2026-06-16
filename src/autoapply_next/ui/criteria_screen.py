"""CriteriaScreen: what jobs the bot searches for (skills + location).

Writes config.yaml's `search` block while preserving everything else (so an
existing match_threshold or candidate block survives). Saves off the GUI thread
via the runner; emits `saved`.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

_SAVE_TOKEN = "criteria_save"


def _config_path(workdir) -> Path:
    return Path(workdir) / "config.yaml"


def _read_config(workdir) -> dict:
    p = _config_path(workdir)
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("criteria: config read failed: %s", exc)
        return {}


def load_criteria(workdir) -> tuple[list[str], str]:
    search = _read_config(workdir).get("search")
    search = search if isinstance(search, dict) else {}
    skills = [str(s) for s in (search.get("skills") or [])]
    return skills, str(search.get("location") or "")


def save_criteria(workdir, skills: list[str], location: str) -> None:
    cfg = _read_config(workdir)
    search = cfg.get("search")
    if not isinstance(search, dict):
        search = {}
    search["skills"] = list(skills)
    search["location"] = location
    cfg["search"] = search
    _config_path(workdir).write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _parse_skills(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


class CriteriaScreen(QWidget):
    saved = Signal()

    def __init__(self, *, engine_workdir, runner):
        super().__init__()
        self._engine_workdir = Path(engine_workdir)
        self._runner = runner
        self._runner.succeeded.connect(self._on_succeeded)
        self._runner.failed.connect(self._on_failed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 36, 36, 36)
        layout.setSpacing(12)

        title = QLabel("What should AutoApply look for?")
        title.setFont(_h1())
        layout.addWidget(title)
        intro = QLabel("Tell it the roles you want and where. You can change this any time.")
        intro.setStyleSheet("color: #6b7280;")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self._skills = QLineEdit()
        self._skills.setPlaceholderText("e.g. AWS, DevOps, Cloud Engineer")
        form.addRow(QLabel("Keywords or job titles"), self._skills)
        self._location = QLineEdit()
        self._location.setPlaceholderText("e.g. Sydney, or Australia")
        form.addRow(QLabel("Location"), self._location)
        layout.addLayout(form)

        self._error = QLabel("")
        self._error.setStyleSheet("color: #b91c1c;")
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        layout.addWidget(self._error)

        self._save_btn = QPushButton("Save and continue")
        self._save_btn.setStyleSheet(_primary_btn())
        self._save_btn.clicked.connect(self.submit_save)
        layout.addWidget(self._save_btn)
        layout.addStretch(1)

        self._load()

    # -------------------------------------------------------- public/test API
    def set_criteria(self, skills_text: str, location: str) -> None:
        self._skills.setText(skills_text)
        self._location.setText(location)

    def skills_text(self) -> str:
        return self._skills.text()

    def location_text(self) -> str:
        return self._location.text().strip()

    def error_text(self) -> str:
        return self._error.text()

    def submit_save(self) -> None:
        skills = _parse_skills(self._skills.text())
        location = self._location.text().strip()
        if not skills or not location:
            self._show_error("Please add at least one keyword and a location.")
            return
        self._clear_error()
        self._save_btn.setEnabled(False)
        wd = self._engine_workdir
        self._runner.submit(lambda: save_criteria(wd, skills, location), token=_SAVE_TOKEN)

    # -------------------------------------------------------- callbacks
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
        self._show_error(f"We couldn't save that. {message}")

    # -------------------------------------------------------- internals
    def _load(self) -> None:
        skills, location = load_criteria(self._engine_workdir)
        if skills:
            self._skills.setText(", ".join(skills))
        if location:
            self._location.setText(location)

    def _show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.setVisible(True)

    def _clear_error(self) -> None:
        self._error.clear()
        self._error.setVisible(False)


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f


def _primary_btn() -> str:
    return (
        "QPushButton { background: #1d4ed8; color: white; padding: 10px 16px; "
        "border-radius: 6px; font-weight: bold; }"
        "QPushButton:disabled { background: #93c5fd; color: #e0e7ff; }"
    )
