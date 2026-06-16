"""M-D: run-state vocabulary + user-facing copy.

The overall bot status (distinct from per-job ProgressStage) is a small RunState
vocabulary, each with a plain, reassuring status line. Also home to the three
cooldown messages, the held-queue "Waiting on you" prompt, and the onboarding
honesty line. Tests assert STRUCTURE (so a copy/humanize pass won't break them)
and enforce the no-em-dash house rule.
"""

from __future__ import annotations

import pytest

from autoapply_next.ui import run_status as rs
from autoapply_next.ui.run_status import RunState


def test_every_run_state_has_a_nonempty_status_line():
    for state in RunState:
        line = rs.status_line(state)
        assert isinstance(line, str) and line.strip()


def test_applying_line_can_show_progress_counts():
    line = rs.status_line(RunState.APPLYING, done=3, total=12)
    assert "3" in line and "12" in line


def test_applying_line_without_counts_still_works():
    assert rs.status_line(RunState.APPLYING).strip()  # no ctx -> generic, no crash


def test_exactly_three_cooldown_messages():
    assert len(rs.COOLDOWN_MESSAGES) == 3
    assert all(isinstance(m, str) and m.strip() for m in rs.COOLDOWN_MESSAGES)


def test_cooldown_message_rotates_by_index():
    assert rs.cooldown_message(0) == rs.COOLDOWN_MESSAGES[0]
    assert rs.cooldown_message(3) == rs.COOLDOWN_MESSAGES[0]  # wraps
    assert rs.cooldown_message(4) == rs.COOLDOWN_MESSAGES[1]


def test_honesty_line_and_waiting_prompt_present():
    assert rs.HONESTY_LINE.strip()
    assert rs.WAITING_ON_YOU_PROMPT.strip()


def test_daily_limit_line_reassures_about_tomorrow():
    assert "tomorrow" in rs.status_line(RunState.DAILY_LIMIT).lower()


def test_no_em_dashes_anywhere_in_copy():
    blobs = [
        rs.HONESTY_LINE,
        rs.WAITING_ON_YOU_PROMPT,
        *rs.COOLDOWN_MESSAGES,
        *[rs.status_line(s) for s in RunState],
    ]
    for text in blobs:
        assert "—" not in text, f"em dash found in: {text!r}"
