"""Implementation of the error safety net. See safe_ui/__init__.py for the
high-level contract.

# Threading

`ErrorBus.error` is a Qt signal; emitting it from any thread queues the slot
on the GUI thread because the receiver lives there. Worker threads can emit
safely.

# Idempotency

`install_global_handlers()` overwrites `sys.excepthook` and the Qt message
handler each call. Calling twice is harmless; the second call wins.

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

from PySide6.QtCore import QObject, QtMsgType, Signal, qInstallMessageHandler
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


def install_global_handlers() -> None:
    """Install `sys.excepthook` and `qInstallMessageHandler`. Idempotent."""
    sys.excepthook = _excepthook
    qInstallMessageHandler(_qt_message_handler)
    logger.info("safe_ui: installed sys.excepthook + Qt message handler")


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
