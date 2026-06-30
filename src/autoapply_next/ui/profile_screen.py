"""ProfileScreen: edit candidate (name/email/phone) and the resume text profile.

Reads the engine workdir's `config.yaml` and `assets/profile.txt`, edits in
place. The engine reads profile.txt on first invocation per process via a
module-level cache (see `vendor/job-finder/utils.py:11-20`), so the user must
restart the engine worker after a profile change to pick up new content.
This is documented in-screen and is acceptable for Phase 2.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..safe_ui import safe_slot, show_error_dialog

logger = logging.getLogger(__name__)


class ProfileScreen(QWidget):
    def __init__(self, *, engine_workdir: Path):
        super().__init__()
        self._engine_workdir = engine_workdir
        self._config_path = engine_workdir / "config.yaml"
        self._profile_path = engine_workdir / "assets" / "profile.txt"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Profile")
        title.setFont(_h1())
        layout.addWidget(title)

        layout.addWidget(self._build_candidate_block())
        layout.addWidget(self._build_resume_block(), stretch=1)
        layout.addWidget(self._build_save_row())

        self._load()

    # ---------------------------------------------------------- widget build

    def _build_candidate_block(self) -> QWidget:
        block = QFrame()
        block.setObjectName("candidate-block")
        block.setStyleSheet(
            "QFrame#candidate-block { border: 1px solid #d1d5db; border-radius: 8px; padding: 12px; }"
        )
        form = QFormLayout(block)
        form.setContentsMargins(16, 16, 16, 16)
        self._name = QLineEdit()
        self._email = QLineEdit()
        self._phone = QLineEdit()
        form.addRow("Name:", self._name)
        form.addRow("Email:", self._email)
        form.addRow("Phone:", self._phone)
        return block

    def _build_resume_block(self) -> QWidget:
        block = QFrame()
        block.setObjectName("resume-block")
        v = QVBoxLayout(block)
        v.setContentsMargins(0, 0, 0, 0)
        label = QLabel(
            "Resume profile (this text is what the matcher and tailorer read). "
            "Sections: PROFESSIONAL SUMMARY, CORE SKILLS, PROFESSIONAL EXPERIENCE."
        )
        label.setWordWrap(True)
        label.setStyleSheet("color: #6b7280; font-size: 12px;")
        v.addWidget(label)
        self._resume = QTextEdit()
        self._resume.setAcceptRichText(False)
        self._resume.setStyleSheet(
            "QTextEdit { font-family: 'SF Mono', 'Cascadia Code', Consolas, monospace; }"
        )
        v.addWidget(self._resume, stretch=1)
        return block

    def _build_save_row(self) -> QWidget:
        row = QHBoxLayout()
        wrap = QWidget()
        wrap.setLayout(row)
        row.addStretch(1)
        self._save_btn = QPushButton("Save profile")
        self._save_btn.setProperty("buttonRole", "primary")
        self._save_btn.clicked.connect(self._save)
        row.addWidget(self._save_btn)
        return wrap

    # ---------------------------------------------------------- behaviour

    def _load(self) -> None:
        try:
            cfg = yaml.safe_load(self._config_path.read_text()) or {}
            cand = cfg.get("candidate") or {}
            self._name.setText(str(cand.get("name", "")))
            self._email.setText(str(cand.get("email", "")))
            self._phone.setText(str(cand.get("phone", "")))
        except Exception as exc:
            logger.warning("ProfileScreen: failed to load config.yaml: %s", exc)
        try:
            self._resume.setPlainText(self._profile_path.read_text())
        except Exception as exc:
            logger.warning("ProfileScreen: failed to load profile.txt: %s", exc)

    @Slot()
    @safe_slot
    def _save(self) -> None:
        # Validate inputs visibly before writing anything to disk.
        name = self._name.text().strip()
        email = self._email.text().strip()
        phone = self._phone.text().strip()
        # Keep the resume content exactly as typed (trailing newlines and
        # all) so the engine sees what the user saw.
        resume = self._resume.toPlainText()
        missing: list[str] = []
        if not name:
            missing.append("Name")
        if not email:
            missing.append("Email")
        elif "@" not in email:
            missing.append("Email (looks invalid)")
        if not phone:
            missing.append("Phone")
        if not resume.strip():
            missing.append("Resume profile text")
        if missing:
            show_error_dialog(
                self,
                "Profile incomplete",
                "Please fill in:\n  - " + "\n  - ".join(missing) +
                "\n\nThe engine needs all four to run; saving an empty profile "
                "would make every job application blow up downstream.",
            )
            return
        # Write atomically: write to a temp file, then replace, so a crash
        # mid-write does not leave a half-written config.
        try:
            cfg_existing = (
                yaml.safe_load(self._config_path.read_text())
                if self._config_path.exists()
                else {}
            )
            cfg = cfg_existing or {}
            cfg.setdefault("candidate", {})
            cfg["candidate"]["name"] = name
            cfg["candidate"]["email"] = email
            cfg["candidate"]["phone"] = phone
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cfg = self._config_path.with_suffix(self._config_path.suffix + ".tmp")
            tmp_cfg.write_text(yaml.safe_dump(cfg, sort_keys=False))
            tmp_cfg.replace(self._config_path)
            self._profile_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_profile = self._profile_path.with_suffix(self._profile_path.suffix + ".tmp")
            tmp_profile.write_text(resume)
            tmp_profile.replace(self._profile_path)
            QMessageBox.information(
                self,
                "Saved",
                "Profile saved. Restart the app to pick up the changes "
                "(the engine caches profile.txt per process).",
            )
        except Exception as exc:
            logger.exception("ProfileScreen: save failed")
            show_error_dialog(
                self,
                "Save failed",
                f"Could not save the profile: {exc}",
            )


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f
