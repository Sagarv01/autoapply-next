"""M-C.3: answering a held question remembers it + unblocks every waiting job.

answer_and_unblock persists the answer to the held-queue bank (so the resolver
reuses it on FUTURE jobs, not just once), and re-queues every parked job that was
waiting on that exact question.
"""

from __future__ import annotations

import sqlite3

from autoapply_next.screening.answer_flow import answer_and_unblock
from autoapply_next.screening.held_queue import HeldQueue


def _make_db(tmp_path, rows):
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("CREATE TABLE applications (url TEXT PRIMARY KEY, status TEXT)")
    conn.executemany("INSERT INTO applications (url, status) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _status(tmp_path, url):
    conn = sqlite3.connect(tmp_path / "jobs.db")
    row = conn.execute("SELECT status FROM applications WHERE url=?", (url,)).fetchone()
    conn.close()
    return row[0] if row else None


def test_answer_remembers_for_future_and_requeues_all_waiting(tmp_path):
    held_path = tmp_path / "held_questions.json"
    q = HeldQueue()
    q.hold("12345678", "Why do you want this role?")
    q.hold("87654321", "Why do you want this role?")  # second job, same question
    q.save(held_path)
    _make_db(tmp_path, [
        ("https://www.seek.com.au/job/12345678", "held"),
        ("https://www.seek.com.au/job/87654321", "held"),
    ])

    out = answer_and_unblock(tmp_path, "Why do you want this role?", "Your mission resonates with me.")

    assert set(out["unblocked"]) == {"12345678", "87654321"}
    assert out["requeued"] == 2
    # both parked jobs are re-queued
    assert _status(tmp_path, "https://www.seek.com.au/job/12345678") == "queued"
    assert _status(tmp_path, "https://www.seek.com.au/job/87654321") == "queued"
    # remembered in the bank -> reused on future jobs (case/space-insensitive)
    reloaded = HeldQueue.load(held_path)
    assert reloaded.known_answer("  why do you want this role? ") == "Your mission resonates with me."
    assert reloaded.pending() == []


def test_unknown_question_is_a_noop(tmp_path):
    out = answer_and_unblock(tmp_path, "never asked", "x")
    assert out["unblocked"] == [] and out["requeued"] == 0
