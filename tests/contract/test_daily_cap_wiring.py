"""M-D: the invisible 100/day submission cap is always on for live runs.

The cap is not user-configurable; the worker always hands run_batch a 100/day
cap plus a today_count_fn (per-DAY across runs) on live submits. Dry-run files
nothing, so it is never capped. A smaller explicit cap (tests) is honored but can
never exceed the invisible ceiling.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from autoapply_next.engine.worker import DAILY_SUBMIT_CAP, daily_cap_kwargs


def test_invisible_cap_is_100():
    assert DAILY_SUBMIT_CAP == 100


def test_live_run_gets_cap_and_today_count(tmp_path):
    kw = daily_cap_kwargs(tmp_path, allow_real_submit=True)
    assert kw["daily_cap"] == 100
    assert callable(kw["today_count_fn"])
    assert kw["today_count_fn"]() == 0  # empty workdir, no jobs.db


def test_dry_run_is_uncapped(tmp_path):
    assert daily_cap_kwargs(tmp_path, allow_real_submit=False) == {}


def test_smaller_requested_cap_is_honored(tmp_path):
    assert daily_cap_kwargs(tmp_path, allow_real_submit=True, requested=10)["daily_cap"] == 10


def test_requested_cap_cannot_exceed_ceiling(tmp_path):
    assert daily_cap_kwargs(tmp_path, allow_real_submit=True, requested=500)["daily_cap"] == 100


def test_today_count_fn_reads_jobs_db(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE applications (url TEXT PRIMARY KEY, status TEXT, timestamp TEXT)"
    )
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("INSERT INTO applications VALUES ('u1','applied',?)", (f"{today} 09:00:00",))
    conn.execute("INSERT INTO applications VALUES ('u2','submitted_uncertain',?)", (f"{today} 10:00:00",))
    conn.commit()
    conn.close()
    kw = daily_cap_kwargs(tmp_path, allow_real_submit=True)
    assert kw["today_count_fn"]() == 2
