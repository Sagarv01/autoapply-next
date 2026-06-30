"""SignInScreen: real Supabase email + password sign-in.

Auth runs OFF the GUI thread via an injected AsyncTaskRunner (AuthManager.sign_in
is a blocking httpx call; running it on the GUI thread would freeze the wizard,
which reads as a crash). The runner hands the result back through Qt signals,
delivered on the GUI thread; on success we emit `authenticated(user_id)`, on
failure we show a message and stay usable.

VOICE: the user-facing strings here are plain, warm drafts for the humanize pass.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# Token namespace for this screen, so a shared runner can disambiguate callbacks.
_TOKEN_SIGNIN = "signin"
_TOKEN_SIGNUP = "signup"
_MY_TOKENS = (_TOKEN_SIGNIN, _TOKEN_SIGNUP)


class SignInScreen(QWidget):
    """Email + password sign-in. Emits `authenticated(user_id)` on success."""

    authenticated = Signal(str)

    def __init__(self, *, auth_manager, runner):
        super().__init__()
        self._auth = auth_manager
        self._runner = runner
        self._runner.succeeded.connect(self._on_succeeded)
        self._runner.failed.connect(self._on_failed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 48, 48, 48)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(14)

        title = QLabel("Sign in to AutoApply")
        title.setFont(_h1())
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Use the email and password for your AutoApply account.")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: #6b7280;")
        layout.addWidget(subtitle)

        self._email = QLineEdit()
        self._email.setPlaceholderText("you@example.com")
        self._email.setMaximumWidth(360)
        layout.addWidget(self._email, alignment=Qt.AlignCenter)

        self._password = QLineEdit()
        self._password.setPlaceholderText("password")
        self._password.setEchoMode(QLineEdit.Password)
        self._password.setMaximumWidth(360)
        self._password.returnPressed.connect(self.submit_sign_in)
        layout.addWidget(self._password, alignment=Qt.AlignCenter)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setAlignment(Qt.AlignCenter)
        self._error.setStyleSheet("color: #b91c1c;")
        self._error.setVisible(False)
        layout.addWidget(self._error)

        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        self._signin_btn = QPushButton("Sign in")
        self._signin_btn.setMinimumWidth(160)
        self._signin_btn.setProperty("buttonRole", "primary")
        self._signin_btn.clicked.connect(self.submit_sign_in)
        row.addWidget(self._signin_btn)
        self._signup_btn = QPushButton("Create account")
        self._signup_btn.setMinimumWidth(160)
        self._signup_btn.clicked.connect(self.submit_sign_up)
        row.addWidget(self._signup_btn)
        layout.addLayout(row)

    # ------------------------------------------------------- public actions
    def submit_sign_in(self) -> None:
        email, password = self._read_credentials()
        if not self._validate(email, password):
            return
        self._begin_busy()
        self._runner.submit(lambda: self._auth.sign_in(email, password), token=_TOKEN_SIGNIN)

    def submit_sign_up(self) -> None:
        email, password = self._read_credentials()
        if not self._validate(email, password):
            return
        self._begin_busy()
        self._runner.submit(lambda: self._auth.sign_up(email, password), token=_TOKEN_SIGNUP)

    # ------------------------------------------------------- runner callbacks
    @Slot(object, object)
    def _on_succeeded(self, result, token) -> None:
        if token not in _MY_TOKENS:
            return
        self._end_busy()
        if token == _TOKEN_SIGNUP and result is None:
            # Supabase requires email confirmation before the session is active.
            self._show_error(
                "Almost there. Check your email to confirm your account, then sign in."
            )
            return
        user_id = getattr(result, "user_id", None)
        if not user_id:
            self._show_error("We couldn't sign you in just now. Please try again.")
            return
        self._clear_error()
        self.authenticated.emit(user_id)

    @Slot(str, object)
    def _on_failed(self, message, token) -> None:
        if token not in _MY_TOKENS:
            return
        self._end_busy()
        self._show_error(f"We couldn't sign you in. {_clean(message)}")

    # ------------------------------------------------------- test/UI helpers
    def set_credentials(self, email: str, password: str) -> None:
        self._email.setText(email)
        self._password.setText(password)

    def error_text(self) -> str:
        # The label's text is the source of truth (cleared to "" when no error);
        # isVisible() is unreliable in headless tests where the screen isn't shown.
        return self._error.text()

    def is_interactive(self) -> bool:
        return self._signin_btn.isEnabled() and self._email.isEnabled()

    # ------------------------------------------------------- internals
    def _read_credentials(self) -> tuple[str, str]:
        # Read widget state on the GUI thread before any submit (never off-thread).
        return self._email.text().strip(), self._password.text()

    def _validate(self, email: str, password: str) -> bool:
        if not email or not password:
            self._show_error("Please enter your email and password.")
            return False
        return True

    def _begin_busy(self) -> None:
        self._clear_error()
        self._set_enabled(False)

    def _end_busy(self) -> None:
        self._set_enabled(True)

    def _set_enabled(self, on: bool) -> None:
        for w in (self._email, self._password, self._signin_btn, self._signup_btn):
            w.setEnabled(on)

    def _show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.setVisible(True)

    def _clear_error(self) -> None:
        self._error.clear()
        self._error.setVisible(False)


def _clean(message: str) -> str:
    """Strip the runner's '<ExcType>: ' prefix so the user sees the plain reason."""
    if ": " in message:
        head, _, tail = message.partition(": ")
        if head.isidentifier() or head.endswith("Error"):
            return tail
    return message


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(22)
    f.setBold(True)
    return f
