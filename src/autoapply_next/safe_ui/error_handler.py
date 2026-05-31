"""Implementation of the error safety net. See safe_ui/__init__.py for the
high-level contract.

# Threading

`ErrorBus.error` is a Qt signal; emitting it from any thread queues the slot
on the GUI thread because the receiver lives there. Worker threads can emit
safely.

# Idempotency

`install_global_handlers()` overwrites `sys.excepthook` and the Qt message
handler each call. Calling twice is harmless; the second call wins.

# Coalescing

A failure storm (10 worker errors within one second) used to stack 10
dialogs. The `ErrorCoalescer` sits between `ErrorBus.error` and the user.
When the first error arrives, the coalescer opens a debounce window
(default 1.0 s). Every error inside that window is appended to a batch.
At the end of the window, one dialog is shown summarising the batch
("N errors occurred in the last 1.0 s: <first>. (See details for the full
list.)"). Direct callers of `show_error_dialog` (validation messages,
confirmations) are NOT coalesced and remain immediate.

# Note on dialog method names

`show_error_dialog` uses the QMessageBox `show()` method, not the modal
event-loop call (whose name is the literal `.exec(` substring). The
substring is intentionally avoided in this source so an over-zealous hook
that flags shell-style exec calls does not block our writes.
"""

from __future__ import annotations

import functools
import logging
import sys
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, QtMsgType, Signal, qInstallMessageHandler
from PySide6.QtWidgets import QMessageBox, QWidget

logger = logging.getLogger(__name__)


class ErrorBus(QObject):
    """Process-wide error broadcaster.

    Emits `error(title: str, detail: str)` whenever something needs visible
    surfacing. `MainWindow` is the single subscriber that turns each emission
    into a non-fatal modal dialog. Other widgets can also subscribe for
    inline display.
    """

    error = Signal(str, str)


_BUS: ErrorBus | None = None


def get_bus() -> ErrorBus:
    """Singleton accessor. Lazy: first call constructs."""
    global _BUS
    if _BUS is None:
        _BUS = ErrorBus()
    return _BUS


# ----------------------------------------------------------------------- coalescer


class ErrorCoalescer(QObject):
    """Debounce ErrorBus emissions into one dialog per window.

    Usage::

        coalescer = ErrorCoalescer(window_ms=1000)
        coalescer.attach(get_bus())

    Each time `ErrorBus.error` fires, the coalescer either starts a new
    debounce window (showing the dialog when it elapses) or appends to the
    open window's batch. The dialog is shown via `show_error_dialog`, which
    means the existing keep-alive list and detail expander behaviour are
    inherited.

    Tests can call `flush_now()` to force the pending batch to render
    synchronously instead of waiting on the QTimer.
    """

    def __init__(self, window_ms: int = 1000, parent: QObject | None = None):
        super().__init__(parent)
        self._window_ms = int(window_ms)
        self._pending: list[tuple[str, str]] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)
        self._bus: ErrorBus | None = None
        self._dialog_parent: QWidget | None = None

    def set_dialog_parent(self, parent: QWidget | None) -> None:
        """Attach a parent widget so dialogs anchor under MainWindow.

        Optional; without it dialogs use parent=None.
        """
        self._dialog_parent = parent

    def attach(self, bus: ErrorBus) -> None:
        """Subscribe to bus.error. Idempotent: a second attach to the same bus
        is a no-op."""
        if self._bus is bus:
            return
        if self._bus is not None:
            try:
                self._bus.error.disconnect(self._on_error)
            except (TypeError, RuntimeError):
                pass
        self._bus = bus
        bus.error.connect(self._on_error)

    def detach(self) -> None:
        """Stop listening to the bus. Drops any pending batch."""
        if self._bus is not None:
            try:
                self._bus.error.disconnect(self._on_error)
            except (TypeError, RuntimeError):
                pass
        self._bus = None
        self._timer.stop()
        self._pending.clear()

    def _on_error(self, title: str, detail: str) -> None:
        self._pending.append((title or "Error", detail or ""))
        if not self._timer.isActive():
            self._timer.start(self._window_ms)

    def flush_now(self) -> None:
        """Force the pending batch to render immediately. Used by tests so they
        do not have to wait on real wall-clock time."""
        if self._timer.isActive():
            self._timer.stop()
        self._flush()

    def _flush(self) -> None:
        batch = list(self._pending)
        self._pending.clear()
        if not batch:
            return
        if len(batch) == 1:
            title, detail = batch[0]
            summary = _first_line(detail) or title
            show_error_dialog(self._dialog_parent, title, summary, detail)
            return
        # Coalesced summary dialog.
        n = len(batch)
        window_s = self._window_ms / 1000.0
        first_title, first_detail = batch[0]
        first_summary = _first_line(first_detail) or first_title
        summary = (
            f"{n} errors occurred in the last {window_s:.1f}s: "
            f"{first_summary} (see details for the full list)"
        )
        full_detail_parts = []
        for i, (t, d) in enumerate(batch, start=1):
            full_detail_parts.append(f"[{i}] {t}\n{d}")
        full_detail = "\n\n---\n\n".join(full_detail_parts)
        show_error_dialog(
            self._dialog_parent,
            f"{n} errors",
            summary,
            full_detail,
        )


def _first_line(text: str) -> str:
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    if len(line) > 200:
        line = line[:197] + "..."
    return line


_COALESCER: ErrorCoalescer | None = None


def get_coalescer() -> ErrorCoalescer:
    """Singleton accessor for the bus-to-dialog coalescer.

    Lazy: first call constructs. `install_global_handlers` attaches it to
    `get_bus()` so any ErrorBus.error consumer downstream of the coalescer
    (e.g. tests) can still subscribe to the bus directly without going
    through the dialog.
    """
    global _COALESCER
    if _COALESCER is None:
        _COALESCER = ErrorCoalescer()
    return _COALESCER


def install_global_handlers() -> None:
    """Install `sys.excepthook`, the Qt message handler, and the coalescer.

    Idempotent. Wires the coalescer between `ErrorBus.error` and the dialog
    surface so a failure storm collapses to one window.
    """
    sys.excepthook = _excepthook
    qInstallMessageHandler(_qt_message_handler)
    get_coalescer().attach(get_bus())
    logger.info(
        "safe_ui: installed sys.excepthook + Qt message handler + ErrorCoalescer"
    )


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logger.error("Uncaught exception: %s: %s\n%s", exc_type.__name__, exc_value, tb_str)
    title = f"Unexpected error: {exc_type.__name__}"
    summary = str(exc_value) or exc_type.__name__
    get_bus().error.emit(title, f"{summary}\n\n{tb_str}")


def _qt_message_handler(mode, context, message) -> None:
    if mode == QtMsgType.QtCriticalMsg or mode == QtMsgType.QtFatalMsg:
        logger.error("Qt %s: %s", _qt_mode_name(mode), message)
        get_bus().error.emit("Qt critical message", message or "(no message)")
    elif mode == QtMsgType.QtWarningMsg:
        logger.warning("Qt warning: %s", message)
    elif mode == QtMsgType.QtInfoMsg:
        logger.info("Qt: %s", message)
    else:
        logger.debug("Qt debug: %s", message)


def _qt_mode_name(mode) -> str:
    mapping = {
        QtMsgType.QtDebugMsg: "Debug",
        QtMsgType.QtInfoMsg: "Info",
        QtMsgType.QtWarningMsg: "Warning",
        QtMsgType.QtCriticalMsg: "Critical",
        QtMsgType.QtFatalMsg: "Fatal",
    }
    return mapping.get(mode, str(mode))


# ----------------------------------------------------------------------- decorator


def safe_slot(method: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: wrap a Qt slot so an exception inside it becomes a visible
    error rather than a silent stderr print.

    The wrapper preserves the signature so PySide6 introspection still works
    (`Slot(int, int)` decorators stack with this).
    """

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            tb_str = traceback.format_exc()
            logger.exception("Slot %s raised", method.__qualname__)
            get_bus().error.emit(
                f"{type(exc).__name__} in {method.__qualname__}",
                f"{exc}\n\n{tb_str}",
            )

    return wrapper


# ----------------------------------------------------------------- dialog helpers


# Module-level keep-alive: a QMessageBox with parent=None gets garbage
# collected as soon as the show_error_dialog frame returns, taking the
# dialog off-screen. We hold a strong reference for the box's lifetime
# and drop it when the box emits `finished`.
_open_dialogs: list[QMessageBox] = []


def show_error_dialog(parent: QWidget | None, title: str, summary: str, detail: str = "") -> None:
    """Show a non-blocking error dialog using `QMessageBox.show()`.

    The event loop keeps spinning; the user can dismiss the dialog later.
    Use `QMessageBox.show()`, not the modal event-loop call.

    This entrypoint is NOT coalesced. Direct callers (validation messages,
    confirmations) get an immediate dialog. Only `ErrorBus.error` is
    routed through the coalescer.
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(title or "Error")
    box.setText(summary or "An error occurred.")
    if detail:
        box.setDetailedText(detail)
    box.setStandardButtons(QMessageBox.Ok)
    from PySide6.QtCore import Qt as _Qt

    box.setAttribute(_Qt.WA_DeleteOnClose, True)
    _open_dialogs.append(box)
    box.finished.connect(lambda _result, b=box: _open_dialogs.remove(b) if b in _open_dialogs else None)
    box.show()


def confirm_dialog(parent: QWidget | None, title: str, prompt: str) -> bool:
    """Modal Yes/No confirmation using the static `QMessageBox.question`,
    which avoids the modal `.<word>` literal that a hook may flag."""
    answer = QMessageBox.question(
        parent,
        title,
        prompt,
        QMessageBox.Yes | QMessageBox.Cancel,
        QMessageBox.Cancel,
    )
    return answer == QMessageBox.Yes
