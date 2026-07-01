"""Read-only access to persisted job metadata from the engine SQLite DB."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def get_title_for_url(db_path: Path, url: str) -> str | None:
    """Return the stored job title for a URL, or None if not found."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT title FROM applications WHERE url = ?",
                (url,),
            ).fetchone()
            if row and row[0]:
                return row[0]
    except Exception:
        pass
    return None


def get_company_for_url(db_path: Path, url: str) -> str | None:
    """Return the stored company name for a URL, or None if not found."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT company FROM applications WHERE url = ?",
                (url,),
            ).fetchone()
            if row and row[0]:
                return row[0]
    except Exception:
        pass
    return None
