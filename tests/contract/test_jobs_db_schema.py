"""First-run bug: a seeded workdir's jobs.db had no `applications` table.

A clean install seeds config.yaml + assets but not the DB schema (the vendored
tracker.init_db that creates it is never called by the desktop app), so the first
scrape/apply hit 'no such table: applications'. ensure_jobs_db_schema creates the
schema; the apply-eligibility read then works on a fresh workdir.
"""

from __future__ import annotations

import sqlite3

from autoapply_next.engine.persistence import (
    ensure_jobs_db_schema,
    queued_urls_for_batch,
)


def _tables(wd):
    conn = sqlite3.connect(wd / "jobs.db")
    try:
        return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    finally:
        conn.close()


def test_creates_applications_and_seen_jobs(tmp_path):
    ensure_jobs_db_schema(tmp_path)
    assert {"applications", "seen_jobs"} <= _tables(tmp_path)
    conn = sqlite3.connect(tmp_path / "jobs.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
    conn.close()
    assert {"url", "status", "timestamp", "failure_count", "match_score"} <= cols


def test_is_idempotent_and_preserves_data(tmp_path):
    ensure_jobs_db_schema(tmp_path)
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("INSERT INTO applications (url, status) VALUES ('u1', 'queued')")
    conn.commit()
    conn.close()
    ensure_jobs_db_schema(tmp_path)  # second call must not wipe or error
    conn = sqlite3.connect(tmp_path / "jobs.db")
    n = conn.execute("SELECT count(*) FROM applications").fetchone()[0]
    conn.close()
    assert n == 1


def test_fixes_empty_zero_byte_jobs_db(tmp_path):
    # reproduce the bug: jobs.db exists but is empty (no tables)
    (tmp_path / "jobs.db").write_bytes(b"")
    ensure_jobs_db_schema(tmp_path)
    assert "applications" in _tables(tmp_path)


def test_eligibility_read_works_after_schema(tmp_path):
    # the exact failure path: queued_urls_for_batch on a fresh db must not raise
    ensure_jobs_db_schema(tmp_path)
    assert queued_urls_for_batch(engine_workdir=tmp_path, min_score=0) == []


def test_seed_engine_workdir_creates_schema(tmp_path):
    # the first-run path end to end: seeding a fresh workdir leaves a usable db
    from autoapply_next.platform.bootstrap import seed_engine_workdir

    seed_engine_workdir(tmp_path)
    assert "applications" in _tables(tmp_path)
    assert queued_urls_for_batch(engine_workdir=tmp_path, min_score=0) == []
