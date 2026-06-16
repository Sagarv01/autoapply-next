"""M-C.3 tail: answering a held question re-queues the parked jobs.

A held job sits at jobs.db status 'held' (excluded from the batch). When the user
answers the blocking question, the jobs that were waiting on it must flip back to
'queued' so the next batch re-applies them (now with the remembered answer).
"""

from __future__ import annotations

import sqlite3

from autoapply_next.engine.persistence import requeue_held_jobs


def _make_db(tmp_path, rows):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE applications (url TEXT PRIMARY KEY, status TEXT)")
    conn.executemany("INSERT INTO applications (url, status) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return tmp_path


def _status(tmp_path, url):
    conn = sqlite3.connect(tmp_path / "jobs.db")
    row = conn.execute("SELECT status FROM applications WHERE url=?", (url,)).fetchone()
    conn.close()
    return row[0] if row else None


def test_requeues_held_rows_matching_job_id(tmp_path):
    wd = _make_db(tmp_path, [
        ("https://www.seek.com.au/job/12345678", "held"),
        ("https://www.seek.com.au/job/99999999", "held"),
    ])
    n = requeue_held_jobs(wd, ["12345678"])
    assert n == 1
    assert _status(wd, "https://www.seek.com.au/job/12345678") == "queued"
    assert _status(wd, "https://www.seek.com.au/job/99999999") == "held"  # untouched


def test_only_held_rows_are_requeued(tmp_path):
    # a same-id row that already 'applied' must not be reopened
    wd = _make_db(tmp_path, [("https://www.seek.com.au/job/777", "applied")])
    assert requeue_held_jobs(wd, ["777"]) == 0
    assert _status(wd, "https://www.seek.com.au/job/777") == "applied"


def test_empty_or_missing_is_safe(tmp_path):
    assert requeue_held_jobs(tmp_path, ["1"]) == 0  # no db
    wd = _make_db(tmp_path, [("u", "held")])
    assert requeue_held_jobs(wd, []) == 0  # no ids


def test_multiple_ids(tmp_path):
    wd = _make_db(tmp_path, [
        ("https://www.seek.com.au/job/111", "held"),
        ("https://www.seek.com.au/job/222", "held"),
    ])
    assert requeue_held_jobs(wd, ["111", "222"]) == 2
    assert _status(wd, "https://www.seek.com.au/job/111") == "queued"
    assert _status(wd, "https://www.seek.com.au/job/222") == "queued"
