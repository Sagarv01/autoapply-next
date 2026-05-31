"""Tests for ErrorCoalescer (Workstream E).

The coalescer sits between `ErrorBus.error` and the dialog surface so a
failure storm collapses to one dialog per debounce window. Direct calls to
`show_error_dialog` are NOT coalesced and remain immediate.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from autoapply_next.safe_ui.error_handler import (
    ErrorBus,
    ErrorCoalescer,
    show_error_dialog,
)


def _open_message_boxes() -> list[QMessageBox]:
    """Return every currently-open QMessageBox top-level widget."""
    return [
        w for w in QApplication.topLevelWidgets()
        if isinstance(w, QMessageBox) and w.isVisible()
    ]


def _close_all_message_boxes() -> None:
    for box in _open_message_boxes():
        box.close()


@pytest.fixture
def fresh_bus(qtbot) -> ErrorBus:
    """A clean ErrorBus per test, isolated from the process singleton.

    The module-level `_BUS` accumulates slot connections across tests; using
    a fresh QObject avoids ordering surprises.
    """
    bus = ErrorBus()
    yield bus
    # Cleanup any open boxes so later tests start blank.
    _close_all_message_boxes()
    qtbot.wait(20)


@pytest.fixture
def coalescer(qtbot, fresh_bus) -> ErrorCoalescer:
    """An ErrorCoalescer with a short window for test speed."""
    c = ErrorCoalescer(window_ms=100)
    c.attach(fresh_bus)
    yield c
    c.detach()


# ----------------------------------------------------------------- single error


def test_single_error_in_window_one_dialog(qtbot, fresh_bus, coalescer):
    """One bus.error within the window -> one QMessageBox after flush."""
    fresh_bus.error.emit("solo title", "solo detail line 1")
    coalescer.flush_now()
    qtbot.wait(20)

    boxes = _open_message_boxes()
    assert len(boxes) == 1, f"expected 1 dialog, got {len(boxes)}"
    box = boxes[0]
    # Direct title preserved when there is only one error.
    assert "solo" in box.windowTitle().lower() or "solo" in box.text().lower()


# ----------------------------------------------------------------- two errors


def test_two_errors_in_window_one_summary_dialog(qtbot, fresh_bus, coalescer):
    """Two bus.error within the window -> one dialog whose text says '2 errors'."""
    fresh_bus.error.emit("err A", "detail A")
    fresh_bus.error.emit("err B", "detail B")
    coalescer.flush_now()
    qtbot.wait(20)

    boxes = _open_message_boxes()
    assert len(boxes) == 1, f"expected 1 coalesced dialog, got {len(boxes)}"
    box = boxes[0]
    text = box.text()
    assert "2 errors" in text, f"summary did not say '2 errors'; text was: {text!r}"
    # Both detail bodies should be in the expander.
    detail = box.detailedText()
    assert "detail A" in detail and "detail B" in detail


# ----------------------------------------------------------------- separate windows


def test_errors_in_separate_windows_two_dialogs(qtbot, fresh_bus, coalescer):
    """Two flushes in two separate windows -> two QMessageBoxes."""
    fresh_bus.error.emit("first", "1")
    coalescer.flush_now()
    qtbot.wait(20)
    assert len(_open_message_boxes()) == 1

    fresh_bus.error.emit("second", "2")
    coalescer.flush_now()
    qtbot.wait(20)

    boxes = _open_message_boxes()
    assert len(boxes) == 2, f"expected 2 separate dialogs, got {len(boxes)}"


# ----------------------------------------------------------------- direct dialog


def test_direct_show_error_dialog_not_coalesced(qtbot, fresh_bus, coalescer):
    """Direct callers of `show_error_dialog` bypass the coalescer; two direct
    calls produce two dialogs immediately."""
    show_error_dialog(None, "direct 1", "summary 1", "detail 1")
    show_error_dialog(None, "direct 2", "summary 2", "detail 2")
    qtbot.wait(20)

    boxes = _open_message_boxes()
    assert len(boxes) == 2, (
        f"direct show_error_dialog calls should not be coalesced; "
        f"got {len(boxes)} dialogs"
    )
