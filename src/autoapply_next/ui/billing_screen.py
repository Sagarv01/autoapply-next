"""BillingScreen: show the current plan and launch the Stripe upgrade.

Entitlement is fetched off the GUI thread (fetch_subscription_status is async),
and "Upgrade to Pro" runs checkout_flow.run_checkout off-thread: it opens the
browser via QDesktopServices.openUrl, waits on the 127.0.0.1 loopback for the
return, and polls the proxy until the webhook flips the tier. The screen reflects
the outcome (upgraded / pending / canceled). Everything blocking goes through the
injected AsyncTaskRunner; the GUI thread only updates labels.

VOICE: the status strings are plain drafts for the humanize pass.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from ..billing import checkout_flow, proxy_billing

logger = logging.getLogger(__name__)

_STATUS_TOKEN = "billing_status"
_CHECKOUT_TOKEN = "billing_checkout"

_PAID_TIERS = ("basic", "pro")


def _open_external(url: str) -> None:
    from PySide6.QtCore import QUrl

    QDesktopServices.openUrl(QUrl(url))


class BillingScreen(QWidget):
    status_loaded = Signal()   # entitlement fetch finished
    checkout_done = Signal()   # a checkout attempt finished (any outcome)

    def __init__(self, *, runner, fetch_status=None, run_checkout=None, open_url=None):
        super().__init__()
        self._runner = runner
        self._fetch_status = fetch_status or proxy_billing.fetch_subscription_status
        self._run_checkout = run_checkout or checkout_flow.run_checkout
        self._open_url = open_url or _open_external
        self._tier = None
        self._upgrade_visible = False

        self._runner.succeeded.connect(self._on_succeeded)
        self._runner.failed.connect(self._on_failed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 48, 48, 48)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(16)

        title = QLabel("Your plan")
        title.setFont(_h1())
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self._status = QLabel("Checking your plan...")
        self._status.setWordWrap(True)
        self._status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._status)

        self._upgrade_btn = QPushButton("Upgrade to Pro")
        self._upgrade_btn.setStyleSheet(_primary_btn())
        self._upgrade_btn.setVisible(False)
        self._upgrade_btn.clicked.connect(self.start_upgrade)
        layout.addWidget(self._upgrade_btn, alignment=Qt.AlignCenter)

        self._hint = QLabel(
            "Pro tailors your resume and cover letter for every job. Upgrading "
            "opens a secure Stripe checkout in your browser."
        )
        self._hint.setWordWrap(True)
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setStyleSheet("color: #6b7280;")
        layout.addWidget(self._hint)

    # -------------------------------------------------------- public/test API
    def refresh(self) -> None:
        self._status.setText("Checking your plan...")
        self._runner.submit_coro(self._fetch_status(), token=_STATUS_TOKEN)

    def start_upgrade(self) -> None:
        self._upgrade_btn.setEnabled(False)
        self._status.setText("Opening secure checkout in your browser...")
        opener = self._open_url
        self._runner.submit_coro(
            self._run_checkout(plan="pro", open_url=opener), token=_CHECKOUT_TOKEN
        )

    def can_upgrade(self) -> bool:
        # Track shown state explicitly: isVisible() is False in headless tests
        # where the screen isn't shown.
        return self._upgrade_visible and self._upgrade_btn.isEnabled()

    def status_text(self) -> str:
        return self._status.text()

    # -------------------------------------------------------- callbacks
    @Slot(object, object)
    def _on_succeeded(self, result, token) -> None:
        if token == _STATUS_TOKEN:
            self._apply_status(result or {})
            self.status_loaded.emit()
        elif token == _CHECKOUT_TOKEN:
            self._apply_checkout(result or {})
            self.checkout_done.emit()

    @Slot(str, object)
    def _on_failed(self, message, token) -> None:
        if token == _STATUS_TOKEN:
            self._status.setText("We couldn't load your plan. Please try again.")
            self.status_loaded.emit()
        elif token == _CHECKOUT_TOKEN:
            self._upgrade_btn.setEnabled(True)
            self._status.setText(f"Checkout didn't start. {message}")
            self.checkout_done.emit()

    # -------------------------------------------------------- internals
    def _apply_status(self, status: dict) -> None:
        self._tier = (status.get("tier") or "free").lower()
        if self._tier == "pro":
            self._status.setText("You're on the Pro plan. Per-job tailoring is on.")
            self._show_upgrade(False)
        elif self._tier == "basic":
            self._status.setText("You're on the Basic plan (unlimited applications).")
            self._show_upgrade(True)
        else:
            remaining = status.get("applications_remaining")
            extra = f" {remaining} applications left." if remaining is not None else ""
            self._status.setText(f"You're on the Free plan.{extra}")
            self._show_upgrade(True)

    def _apply_checkout(self, result: dict) -> None:
        outcome = result.get("outcome")
        if outcome == "upgraded":
            self._apply_status(result.get("status") or {"tier": "pro"})
        elif outcome == "pending":
            # paid, but the webhook hasn't flipped the tier yet
            self._status.setText(
                "Thanks! We're confirming your upgrade. This usually takes a moment."
            )
            self._show_upgrade(False)
        else:  # canceled / timeout / error
            self._upgrade_btn.setEnabled(True)
            self._status.setText("No changes made. You can upgrade whenever you're ready.")
            self._show_upgrade(self._tier not in _PAID_TIERS)

    def _show_upgrade(self, visible: bool) -> None:
        self._upgrade_visible = visible
        self._upgrade_btn.setVisible(visible)
        if visible:
            self._upgrade_btn.setEnabled(True)


def _h1() -> QFont:
    f = QFont()
    f.setPointSize(20)
    f.setBold(True)
    return f


def _primary_btn() -> str:
    return (
        "QPushButton { background: #1d4ed8; color: white; padding: 10px 18px; "
        "border-radius: 6px; font-weight: bold; }"
        "QPushButton:disabled { background: #93c5fd; color: #e0e7ff; }"
    )
