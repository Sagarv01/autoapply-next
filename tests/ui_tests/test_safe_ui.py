"""Tests for the global error safety net (safe_ui).

These do NOT need a live engine. They verify:
- safe_slot routes a slot exception to ErrorBus.
- ErrorBus.error is a real Qt signal that connected slots receive.
- show_error_dialog produces a visible QMessageBox.
- install_global_handlers replaces sys.excepthook and is idempotent.
- _excepthook routes uncaught exceptions to ErrorBus and the logger.
- The Qt message handler routes a `QtCriticalMsg` to ErrorBus.
"""

from __future__ import annotations

import logging
import sys

import pytest
from PySide6.QtCore import QObject, QtMsgType, Signal
from PySide6.QtWidgets import QMessageBox, QPushButton, QWidget

from autoapply_next.safe_ui import (
    confirm_dialog,
    get_bus,
    install_global_handlers,
    safe_slot,
    show_error_dialog,
)
from autoapply_next.safe_ui import error_handler as eh


# ----------------------------------------------------------------- ErrorBus + safe_slot


class _Emitter(QObject):
    """Synthetic QObject whose 'clicked' slot can be wired through safe_slot."""

    fired = Signal(str)


def test_error_bus_emits_to_connected_slots(qtbot):
    received: list[tuple[str, str]] = []
    get_bus().error.connect(lambda t, d: received.append((t, d)))
    get_bus().error.emit("hello", "world")
    qtbot.wait(20)
    assert ("hello", "world") in received


def test_safe_slot_catches_exception_and_emits_bus(qtbot):
    captured: list[tuple[str, str]] = []
    get_bus().error.connect(lambda t, d: captured.append((t, d)))

    @safe_slot
    def boom():
        raise RuntimeError("kaboom")

    boom()
    qtbot.wait(20)
    assert captured, "safe_slot did not surface the exception"
    title, detail = captured[-1]
    assert "RuntimeError" in title
    assert "kaboom" in detail


def test_safe_slot_preserves_return_value_on_success(qtbot):
    @safe_slot
    def ok():
        return 42

    # No exception path; returns method's value.
    assert ok() == 42


def test_safe_slot_decorator_on_qobject_method(qtbot):
    """Smoke test that wrapping a real QObject slot via signal-connect path
    still surfaces exceptions through ErrorBus."""
    captured: list[tuple[str, str]] = []
    get_bus().error.connect(lambda t, d: captured.append((t, d)))

    btn = QPushButton("X")

    @safe_slot
    def on_click():
        raise ValueError("from slot")

    btn.clicked.connect(on_click)
    btn.click()
    qtbot.wait(20)
    assert any("ValueError" in t for t, _ in captured)


# -------------------------------------------------------------------- excepthook


def test_install_global_handlers_replaces_excepthook(qtbot):
    prev = sys.excepthook
    try:
        install_global_handlers()
        assert sys.excepthook is not prev, "excepthook was not replaced"
        # Idempotent: calling twice is fine, second call wins (same impl).
        install_global_handlers()
    finally:
        sys.excepthook = prev


def test_excepthook_emits_bus_and_logs(qtbot, caplog):
    captured: list[tuple[str, str]] = []
    get_bus().error.connect(lambda t, d: captured.append((t, d)))
    caplog.set_level(logging.ERROR)

    try:
        raise RuntimeError("synthetic")
    except RuntimeError:
        exc_info = sys.exc_info()
    eh._excepthook(*exc_info)
    qtbot.wait(20)

    assert captured, "excepthook did not emit on the bus"
    assert any("RuntimeError" in t for t, _ in captured)
    assert any("synthetic" in r.message for r in caplog.records)


def test_qt_message_handler_routes_critical_to_bus(qtbot, caplog):
    captured: list[tuple[str, str]] = []
    get_bus().error.connect(lambda t, d: captured.append((t, d)))
    caplog.set_level(logging.INFO)

    eh._qt_message_handler(QtMsgType.QtCriticalMsg, None, "qt is upset")
    qtbot.wait(20)
    assert any("qt is upset" in d for _, d in captured)

    # Warnings only log, do not surface.
    before = len(captured)
    eh._qt_message_handler(QtMsgType.QtWarningMsg, None, "qt is meh")
    qtbot.wait(20)
    assert len(captured) == before


# -------------------------------------------------------------------- dialog


def test_show_error_dialog_creates_visible_messagebox(qtbot):
    show_error_dialog(None, "title", "summary", "detail line 1\ndetail line 2")
    qtbot.wait(40)
    # Find a QMessageBox in the top-level widgets.
    from PySide6.QtWidgets import QApplication

    boxes = [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox)]
    assert boxes, "show_error_dialog did not create a QMessageBox"
    box = boxes[-1]
    assert box.text() == "summary"
    assert "detail line 1" in box.detailedText()
    box.close()
