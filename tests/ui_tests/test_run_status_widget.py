"""RunStatusWidget: renders the RunState status line + rotates cooldown messages.

It shows status_line(state) for normal states, and during the pacing wait it
rotates the three reassuring cooldown messages so the app never reads as frozen.
"""

from __future__ import annotations

import pytest

from autoapply_next.ui import run_status as rs
from autoapply_next.ui.run_status import RunState
from autoapply_next.ui.run_status_widget import RunStatusWidget


@pytest.fixture
def widget(qtbot):
    w = RunStatusWidget()
    qtbot.addWidget(w)
    return w


def test_set_state_shows_status_line(widget):
    widget.set_state(RunState.SEARCHING)
    assert widget.text() == rs.status_line(RunState.SEARCHING)


def test_applying_shows_progress_counts(widget):
    widget.set_state(RunState.APPLYING, done=3, total=12)
    assert "3" in widget.text() and "12" in widget.text()


def test_cooldown_shows_first_message_then_rotates(widget):
    widget.start_cooldown()
    assert widget.text() == rs.cooldown_message(0)
    widget._tick()
    assert widget.text() == rs.cooldown_message(1)
    widget._tick()
    assert widget.text() == rs.cooldown_message(2)
    widget._tick()
    assert widget.text() == rs.cooldown_message(0)  # wraps


def test_setting_a_state_stops_cooldown(widget):
    widget.start_cooldown()
    assert widget.is_cooling_down()
    widget.set_state(RunState.IDLE)
    assert not widget.is_cooling_down()
    assert widget.text() == rs.status_line(RunState.IDLE)


def test_daily_limit_state(widget):
    widget.set_state(RunState.DAILY_LIMIT)
    assert "tomorrow" in widget.text().lower()
