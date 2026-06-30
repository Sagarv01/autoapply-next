"""DocumentsScreen: install the base resume (required) + cover letter (optional).

The engine reads the base resume from a fixed filename in the workdir's assets/
dir (ob.BASE_RESUME_FILENAME), so we copy the user's chosen file there. The copy
runs off the GUI thread via the runner. The QFileDialog picker is wired to
install_resume/install_cover; tests drive those directly with a path.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..onboarding import state as ob

logger = logging.getLogger(__name__)

_RESUME_TOKEN = "doc_resume"
_COVER_TOKEN = "doc_cover"


def _copy_document(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


class DocumentsScreen(QWidget):
    documents_ready = Signal()  # emitted once the required resume is installed

    def __init__(self, *, engine_workdir, runner):
        super().__init__()
        self._engine_workdir = Path(engine_workdir)
        self._runner = runner
        self._runner.succeeded.connect(self._on_succeeded)
        self._runner.failed.connect(self._on_failed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 36, 36, 36)
        layout.setSpacing(12)

        title = QLabel("Your resume")
        title.setFont(_h1())
        layout.addWidget(title)
        intro = QLabel(
            "Upload the resume AutoApply should send. A Word (.docx) file works "
            "best. You can add a cover letter too, but it's optional."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #6b7280;")
        layout.addWidget(intro)

        self._resume_btn = QPushButton("Upload resume")
        self._resume_btn.setProperty("buttonRole", "primary")
        self._resume_btn.clicked.connect(self._pick_resume)
        layout.addWidget(self._resume_btn)
        self._resume_status = QLabel()
        layout.addWidget(self._resume_status)

        self._cover_btn = QPushButton("Upload cover letter (optional)")
        self._cover_btn.clicked.connect(self._pick_cover)
        layout.addWidget(self._cover_btn)
        self._cover_status = QLabel()
        layout.addWidget(self._cover_status)

        self._error = QLabel("")
        self._error.setStyleSheet("color: #b91c1c;")
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        layout.addWidget(self._error)
        layout.addStretch(1)

        self._refresh_status()

    # -------------------------------------------------------- public/test API
    def has_resume(self) -> bool:
        return self._resume_dest().exists()

    def install_resume(self, path: str) -> None:
        dest = self._resume_dest()
        self._resume_btn.setEnabled(False)
        self._runner.submit(lambda: _copy_document(Path(path), dest), token=_RESUME_TOKEN)

    def install_cover(self, path: str) -> None:
        dest = self._engine_workdir / "assets" / ob.BASE_COVER_FILENAME
        self._cover_btn.setEnabled(False)
        self._runner.submit(lambda: _copy_document(Path(path), dest), token=_COVER_TOKEN)

    # -------------------------------------------------------- pickers (manual)
    def _pick_resume(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose your resume", "", "Documents (*.docx *.pdf *.doc);;All files (*)"
        )
        if path:
            self.install_resume(path)

    def _pick_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose your cover letter", "", "Documents (*.docx *.pdf *.doc);;All files (*)"
        )
        if path:
            self.install_cover(path)

    # -------------------------------------------------------- callbacks
    @Slot(object, object)
    def _on_succeeded(self, result, token) -> None:
        if token == _RESUME_TOKEN:
            self._resume_btn.setEnabled(True)
            self._refresh_status()
            self._clear_error()
            self.documents_ready.emit()
        elif token == _COVER_TOKEN:
            self._cover_btn.setEnabled(True)
            self._refresh_status()
            self._clear_error()

    @Slot(str, object)
    def _on_failed(self, message, token) -> None:
        if token not in (_RESUME_TOKEN, _COVER_TOKEN):
            return
        self._resume_btn.setEnabled(True)
        self._cover_btn.setEnabled(True)
        self._error.setText(f"We couldn't add that file. {message}")
        self._error.setVisible(True)

    # -------------------------------------------------------- internals
    def _resume_dest(self) -> Path:
        return self._engine_workdir / "assets" / ob.BASE_RESUME_FILENAME

    def _refresh_status(self) -> None:
        if self.has_resume():
            self._resume_status.setText("Resume added.")
            self._resume_status.setStyleSheet("color: #166534;")
        else:
            self._resume_status.setText("No resume added yet.")
            self._resume_status.setStyleSheet("color: #6b7280;")
        cover = self._engine_workdir / "assets" / ob.BASE_COVER_FILENAME
        self._cover_status.setText("Cover letter added." if cover.exists() else "")
        self._cover_status.setStyleSheet("color: #166534;")

    def _clear_error(self) -> None:
        self._error.clear()
        self._error.setVisible(False)


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f
