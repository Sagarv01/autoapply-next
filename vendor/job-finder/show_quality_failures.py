"""Show jobs the bot skipped because the cover letter failed the quality gate.

Aggregates by failure marker so you can see the pattern (e.g. "47 jobs tripped
too_long, 12 tripped self_disqualify"). Use this to decide what to fix in the
prompt once a sizeable pool has built up.

Run: venv/bin/python show_quality_failures.py
"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import os

DB = Path(os.environ.get("DB_PATH", "jobs.db"))


def main() -> None:
    if not DB.exists():
        print(f"No tracker DB at {DB.resolve()}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT timestamp, title, company, notes "
        "FROM applications "
        "WHERE status='skipped' AND notes LIKE 'COVER_LETTER_QUALITY_FAIL%' "
        "ORDER BY timestamp DESC"
    ).fetchall()
    conn.close()

    if not rows:
        print("No cover-letter quality-gate failures recorded yet.")
        return

    marker_re = re.compile(r"marker=([^ ]+)")
    by_marker: Counter = Counter()
    by_kind: Counter = Counter()
    for _, _, _, notes in rows:
        m = marker_re.search(notes)
        if m:
            full = m.group(1)
            by_marker[full] += 1
            by_kind[full.split(":", 1)[0]] += 1

    print(f"Total quality-gate failures: {len(rows)}\n")

    print("By kind:")
    for kind, n in by_kind.most_common():
        print(f"  {n:4d}  {kind}")

    print("\nTop 15 specific markers:")
    for marker, n in by_marker.most_common(15):
        print(f"  {n:4d}  {marker}")

    print("\nMost recent 10 failures:")
    for ts, title, company, notes in rows[:10]:
        m = marker_re.search(notes)
        marker = m.group(1) if m else "?"
        print(f"  {ts}  {marker:60s}  {title[:40]} @ {company[:30]}")


if __name__ == "__main__":
    main()
