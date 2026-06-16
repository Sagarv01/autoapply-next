"""M-D: count today's real submissions for the invisible 100/day cap.

The cap must be per-DAY across runs, so the runner needs today's already-applied
count from jobs.db. Only real submissions count (applied + submitted_uncertain);
queued/skipped/failed and prior days do not.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from autoapply_next.engine.persistence import count_today_submissions

_COLS = (
    "url,title,company,board,match_score,match_reasoning,resume_file,"
    "cover_letter_file,status,notes,timestamp,failure_count"
)


def _make_db(tmp_path, rows):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE applications (url TEXT PRIMARY KEY, title TEXT, company TEXT, "
        "board TEXT, match_score INT, match_reasoning TEXT, resume_file TEXT, "
        "cover_letter_file TEXT, status TEXT, notes TEXT, timestamp TEXT, failure_count INT)"
    )
    for i, (status, ts) in enumerate(rows):
        conn.execute(
            f"INSERT INTO applications ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"u{i}", "", "", "seek", 0, "", "", "", status, "", ts, 0),
        )
    conn.commit()
    conn.close()
    return tmp_path


def test_counts_today_applied_and_uncertain(tmp_path):
    today = datetime.now().strftime("%Y-%m-%d")
    yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    wd = _make_db(tmp_path, [
        ("applied", f"{today} 09:00:00"),
        ("applied", f"{today} 10:00:00"),
        ("submitted_uncertain", f"{today} 11:00:00"),
        ("applied", f"{yest} 23:59:00"),    # yesterday -> excluded
        ("queued", f"{today} 12:00:00"),    # not a submission
        ("skipped", f"{today} 12:00:00"),   # not a submission
        ("failed", f"{today} 12:00:00"),    # not a submission
    ])
    assert count_today_submissions(wd) == 3


def test_zero_when_only_prior_days(tmp_path):
    yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    wd = _make_db(tmp_path, [("applied", f"{yest} 10:00:00")])
    assert count_today_submissions(wd) == 0


def test_missing_db_returns_zero(tmp_path):
    assert count_today_submissions(tmp_path) == 0
