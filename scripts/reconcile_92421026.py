"""One-shot reconciliation for the false-negative verify on 2026-05-31.

Background: the user's first real submission (job 92421026, Automation
Engineer @ Hydrogen Group Pty Ltd) succeeded on Seek and is visible on
the Applied Jobs page. The engine's `_verify_applied` returned False
because the adapter passed it placeholder metadata (`title='Seek listing
92421026'`, `company=''`), so the row in jobs.db was left at status
'queued' and the journal entry recorded outcome='failed'. The application
itself is fine; only the record is wrong.

This script flips the DB row to status='applied' and appends a note
explaining the reconciliation. It is idempotent (rerunning it leaves the
row at 'applied').

Run once:
    python scripts/reconcile_92421026.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/Users/sagarverma/Pictures/Claude-experiments/job-finder/jobs.db")
URL = "https://au.seek.com/job/92421026"
RECONCILE_NOTE = (
    "Reconciled 2026-05-31: real submit succeeded (verified on Seek's "
    "Applied Jobs page) but the engine's _verify_applied returned a "
    "false negative due to placeholder title/company. RobustVerifier "
    "wrap now matches by job id to prevent recurrence."
)


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        before = conn.execute(
            "SELECT status, notes, timestamp FROM applications WHERE url = ?",
            (URL,),
        ).fetchone()
        if before is None:
            print(f"No applications row for {URL}; nothing to reconcile.")
            return 1
        print(
            f"BEFORE: status={before['status']!r} "
            f"timestamp={before['timestamp']!r}"
        )
        # Update only status + notes; keep title/company/timestamp/etc.
        new_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE applications SET status = ?, notes = ?, timestamp = ? "
            "WHERE url = ?",
            ("applied", RECONCILE_NOTE, new_ts, URL),
        )
        conn.commit()
        after = conn.execute(
            "SELECT status, notes, timestamp FROM applications WHERE url = ?",
            (URL,),
        ).fetchone()
        print(
            f"AFTER:  status={after['status']!r} "
            f"timestamp={after['timestamp']!r}"
        )
        print(f"notes:  {after['notes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
