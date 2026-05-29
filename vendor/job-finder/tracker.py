import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import openpyxl

from models import Application

DB_PATH = Path(os.environ.get("DB_PATH", "jobs.db"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
_excel_lock = threading.Lock()

# After 3 cumulative failures we stop trying — the failure is structural
# (unanswerable question, broken form, expired posting) not transient.
PERMANENT_FAILURE_THRESHOLD = 3

_APP_COLS = (
    "url, title, company, board, match_score, match_reasoning, "
    "resume_file, cover_letter_file, status, notes, timestamp, failure_count"
)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_jobs (
                url  TEXT PRIMARY KEY,
                seen_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                url               TEXT PRIMARY KEY,
                title             TEXT,
                company           TEXT,
                board             TEXT,
                match_score       INTEGER,
                match_reasoning   TEXT,
                resume_file       TEXT,
                cover_letter_file TEXT,
                status            TEXT,
                notes             TEXT,
                timestamp         TEXT,
                failure_count     INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Idempotent migration for existing DBs created before failure_count.
        try:
            conn.execute(
                "ALTER TABLE applications ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def is_seen(url: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT 1 FROM seen_jobs WHERE url=?", (url,)).fetchone()
    finally:
        conn.close()
    return row is not None


def is_applied_or_skipped(url: str) -> bool:
    """True if this URL is already applied/skipped — don't waste a score on it.
    Failed jobs are NOT skipped so transient failures get retried."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT 1 FROM applications WHERE url=? AND status IN ('applied','skipped')",
            (url,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def mark_seen(url: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO seen_jobs VALUES (?, ?)",
            (url, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_application(app: Application):
    """Persist an application. failure_count is managed automatically:
    incremented on status='failed', reset to 0 on 'applied'/'skipped',
    preserved otherwise. The caller's app.failure_count is overridden."""
    conn = sqlite3.connect(DB_PATH)
    try:
        prev = conn.execute(
            "SELECT failure_count FROM applications WHERE url=?", (app.url,)
        ).fetchone()
        prev_count = prev[0] if prev else 0
        if app.status == "failed":
            new_count = prev_count + 1
        elif app.status in ("applied", "skipped"):
            new_count = 0
        else:
            new_count = prev_count
        app.failure_count = new_count
        conn.execute(
            f"INSERT OR REPLACE INTO applications ({_APP_COLS}) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (app.url, app.title, app.company, app.board, app.match_score,
             app.match_reasoning, app.resume_file, app.cover_letter_file,
             app.status, app.notes, app.timestamp, app.failure_count),
        )
        conn.commit()
    finally:
        conn.close()
    _write_excel_row(app)


def permanently_failed_urls() -> set[str]:
    """URLs that have failed >= PERMANENT_FAILURE_THRESHOLD times. The
    scraper drops these so we don't keep wasting Claude calls on broken
    forms / expired postings."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT url FROM applications WHERE failure_count >= ?",
            (PERMANENT_FAILURE_THRESHOLD,),
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def get_failure_count(url: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT failure_count FROM applications WHERE url=?", (url,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def get_stats() -> dict:
    """Return application counts grouped by status."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM applications GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


def get_stuck_in_progress(minutes: int = 15) -> list[Application]:
    """Return applications that have been in_progress longer than `minutes`."""
    cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            f"SELECT {_APP_COLS} FROM applications "
            "WHERE status='in_progress' AND timestamp < ?",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    return [Application(*row) for row in rows]


def save_queued_job(url: str, title: str, company: str, board: str,
                    match_score: int | None = None, match_reasoning: str | None = None):
    """Persist a job to the DB as 'queued' so it survives bot restarts."""
    app = Application(url=url, title=title, company=company, board=board)
    app.status = "queued"
    if match_score is not None:
        app.match_score = match_score
        app.match_reasoning = match_reasoning or ""
    # Only insert if not already tracked (don't overwrite in_progress/applied etc.)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            f"INSERT OR IGNORE INTO applications ({_APP_COLS}) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (app.url, app.title, app.company, app.board, app.match_score,
             app.match_reasoning, app.resume_file, app.cover_letter_file,
             app.status, app.notes, app.timestamp, app.failure_count),
        )
        conn.commit()
    finally:
        conn.close()


def get_application_by_url(url: str) -> Application | None:
    """Look up the existing application row for this URL (None if absent)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            f"SELECT {_APP_COLS} FROM applications WHERE url=?",
            (url,),
        ).fetchone()
    finally:
        conn.close()
    return Application(*row) if row else None


def get_orphaned_applications() -> list[Application]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            f"SELECT {_APP_COLS} FROM applications "
            "WHERE status IN ('in_progress', 'queued')"
        ).fetchall()
    finally:
        conn.close()
    return [Application(*row) for row in rows]


def _excel_path() -> Path:
    year = datetime.now().year
    return OUTPUT_DIR / f"JobApplications_{year}.xlsx"


def _write_excel_row(app: Application):
    with _excel_lock:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = _excel_path()
        if path.exists():
            wb = openpyxl.load_workbook(path)
            ws = wb.active
        else:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append([
                "Timestamp", "Job Title", "Company", "Board", "Job URL",
                "Match Score", "Resume File", "Cover Letter", "Status", "Notes",
            ])
        ws.append([
            app.timestamp, app.title, app.company, app.board, app.url,
            f"{app.match_score}%", app.resume_file, app.cover_letter_file,
            app.status, app.notes,
        ])
        wb.save(path)
