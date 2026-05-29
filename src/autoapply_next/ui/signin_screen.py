"""SignInScreen: Supabase phone + password sign-in.

For Phase 2 + early Phase 3 this is a stub with a "Skip (dev mode)" button so
the rest of the app is reachable. The real Supabase wiring is in
`autoapply_next.auth.supabase_auth` (slice 1 of Phase 3).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class SignInScreen(QWidget):
    """Stub sign-in. Emits `authenticated` when the user clicks Skip or Sign in."""

    authenticated = Signal(str)
    """Argument: a placeholder user id ("dev-user" for the skip path)."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 48, 48, 48)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(16)

        title = QLabel("Sign in to AutoApply Next")
        title.setFont(_h1())
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel(
            "Phone + password sign-in (Supabase). This screen is a stub during the "
            "walking skeleton. Click Skip to continue."
        )
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: #6b7280;")
        layout.addWidget(subtitle)

        self._phone = QLineEdit()
        self._phone.setPlaceholderText("+61 4xx xxx xxx")
        self._phone.setEnabled(False)
        self._phone.setMaximumWidth(360)
        layout.addWidget(self._phone, alignment=Qt.AlignCenter)

        self._password = QLineEdit()
        self._password.setPlaceholderText("password")
        self._password.setEchoMode(QLineEdit.Password)
        self._password.setEnabled(False)
        self._password.setMaximumWidth(360)
        layout.addWidget(self._password, alignment=Qt.AlignCenter)

        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        signin_btn = QPushButton("Sign in")
        signin_btn.setEnabled(False)
        signin_btn.setMinimumWidth(160)
        signin_btn.setStyleSheet(_primary_btn())
        row.addWidget(signin_btn)
        skip_btn = QPushButton("Skip (dev mode)")
        skip_btn.setMinimumWidth(160)
        skip_btn.setStyleSheet(_secondary_btn())
        skip_btn.clicked.connect(lambda: self.authenticated.emit("dev-user"))
        row.addWidget(skip_btn)
        layout.addLayout(row)

        note = QLabel(
            "Supabase wiring lands in Phase 3 slice 1. The skip button is intentional "
            "until then."
        )
        note.setAlignment(Qt.AlignCenter)
        note.setStyleSheet("color: #9ca3af; font-size: 12px;")
        layout.addWidget(note)


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(22)
    f.setBold(True)
    return f


def _primary_btn() -> str:
    return (
        "QPushButton { background: #1d4ed8; color: white; padding: 10px 16px; "
        "border-radius: 6px; font-weight: bold; }"
        "QPushButton:disabled { background: #93c5fd; color: #e0e7ff; }"
    )


def _secondary_btn() -> str:
    return (
        "QPushButton { background: #f3f4f6; color: #111827; padding: 10px 16px; "
        "border-radius: 6px; border: 1px solid #d1d5db; }"
    )
