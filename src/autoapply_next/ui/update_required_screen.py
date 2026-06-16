"""UpdateRequiredScreen: shown when the app is below the proxy's version floor.

A hard wall: the only action is to download the latest build. MainWindow shows
this (and disables the rest of the app) when the startup version check finds the
running version is below /api/config's min_client_version.

VOICE: plain, warm draft for the humanize pass.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from ..version_check import DOWNLOAD_URL


def _open_external(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


class UpdateRequiredScreen(QWidget):
    def __init__(self, *, open_url=None):
        super().__init__()
        self._open_url = open_url or _open_external

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 48, 48, 48)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(16)

        title = QLabel("Time to update")
        title.setFont(_h1())
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self._message = QLabel(
            "A newer version of AutoApply is needed to keep applying safely. "
            "Download the latest version to continue. It only takes a minute."
        )
        self._message.setWordWrap(True)
        self._message.setAlignment(Qt.AlignCenter)
        self._message.setStyleSheet("color: #374151;")
        layout.addWidget(self._message)

        self._btn = QPushButton("Download the latest version")
        self._btn.setStyleSheet(_primary_btn())
        self._btn.clicked.connect(self.download)
        layout.addWidget(self._btn, alignment=Qt.AlignCenter)

    def download(self) -> None:
        self._open_url(DOWNLOAD_URL)

    def message_text(self) -> str:
        return self._message.text()


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(22)
    f.setBold(True)
    return f


def _primary_btn() -> str:
    return (
        "QPushButton { background: #1d4ed8; color: white; padding: 12px 20px; "
        "border-radius: 6px; font-weight: bold; }"
    )
