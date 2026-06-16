"""HeldQueueScreen: the "Waiting on you" screen for held screening questions.

Shows one pending question at a time with an answer field (or a choice for
single-select). Answering runs answer_and_unblock OFF the GUI thread: it
remembers the answer for future jobs and re-queues every parked job waiting on
it, then advances to the next pending question.
"""

from __future__ import annotations

import sqlite3

import pytest

from autoapply_next.screening.held_queue import HeldQueue
from autoapply_next.ui.async_task import AsyncTaskRunner
from autoapply_next.ui.held_queue_screen import HeldQueueScreen


@pytest.fixture
def runner(qtbot):
    r = AsyncTaskRunner()
    yield r
    r.stop()


def _seed_held(tmp_path, *holds):
    q = HeldQueue()
    for args in holds:
        q.hold(*args[0], **(args[1] if len(args) > 1 else {}))
    q.save(tmp_path / "held_questions.json")


def _make_db(tmp_path, rows):
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("CREATE TABLE applications (url TEXT PRIMARY KEY, status TEXT)")
    conn.executemany("INSERT INTO applications (url, status) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _screen(qtbot, runner, wd):
    s = HeldQueueScreen(engine_workdir=wd, runner=runner)
    qtbot.addWidget(s)
    return s


def test_empty_shows_caught_up(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    s.refresh()
    assert s.pending_count() == 0
    assert "caught up" in s.status_text().lower()


def test_shows_pending_question(qtbot, runner, tmp_path):
    _seed_held(tmp_path, (("111", "Why do you want this role?"),))
    s = _screen(qtbot, runner, tmp_path)
    s.refresh()
    assert s.pending_count() == 1
    assert "Why do you want this role?" in s.current_question()


def test_single_select_offers_options(qtbot, runner, tmp_path):
    _seed_held(tmp_path, (("111", "Shirt size"), {"options": ["S", "M", "L"], "input_type": "single_select"}))
    s = _screen(qtbot, runner, tmp_path)
    s.refresh()
    assert s.current_options() == ["S", "M", "L"]


def test_blank_answer_is_rejected(qtbot, runner, tmp_path):
    _seed_held(tmp_path, (("111", "Why?"),))
    s = _screen(qtbot, runner, tmp_path)
    s.refresh()
    s.set_answer("")
    s.submit_answer()
    assert "answer" in s.status_text().lower()
    # still pending (not consumed)
    assert HeldQueue.load(tmp_path / "held_questions.json").pending()


def test_answering_remembers_requeues_and_advances(qtbot, runner, tmp_path):
    _seed_held(tmp_path, (("111", "Why do you want this role?"),))
    _make_db(tmp_path, [("https://www.seek.com.au/job/111", "held")])
    s = _screen(qtbot, runner, tmp_path)
    s.refresh()
    s.set_answer("Your mission resonates with me.")
    with qtbot.waitSignal(s.answered, timeout=3000):
        s.submit_answer()
    # remembered + job requeued + screen advanced to caught-up
    reloaded = HeldQueue.load(tmp_path / "held_questions.json")
    assert reloaded.known_answer("why do you want this role") == "Your mission resonates with me."
    assert reloaded.pending() == []
    assert s.pending_count() == 0


def test_questions_changed_emitted_on_refresh(qtbot, runner, tmp_path):
    _seed_held(tmp_path, (("111", "Q1?"),), )
    s = _screen(qtbot, runner, tmp_path)
    counts: list = []
    s.questions_changed.connect(counts.append)
    s.refresh()
    assert counts and counts[-1] == 1
