"""Read-only monitor for a live batch.

Two modes:

  --watch                Poll jobs.db every N seconds and print the status
                         of each URL whenever it changes. Prints once at
                         startup so the user sees the baseline before any
                         transition. Default mode.

  --check-eligibility    One-shot. Calls persistence.queued_urls_for_batch
                         and asserts that NONE of the URLs the user passed
                         in are in that list. Prints PASS or FAIL. This is
                         the cross-run-duplicate-guard confirmation.

This script writes nothing. It opens jobs.db read-only and does not import
the adapter, batch, worker, or anything that could submit. The only
imports from the project are read-only helpers in persistence.py.

Usage examples:

    # Watch one URL that you are about to submit live.
    python scripts/watch_live_batch.py \\
        https://au.seek.com/job/92421026

    # Watch a small batch.
    python scripts/watch_live_batch.py \\
        https://au.seek.com/job/AAA https://au.seek.com/job/BBB

    # After the batch, run the eligibility check.
    python scripts/watch_live_batch.py --check-eligibility \\
        https://au.seek.com/job/AAA https://au.seek.com/job/BBB

The engine workdir is `$AUTOAPPLY_NEXT_ENGINE_WORKDIR` if set, else the
default location from `autoapply_next.platform.paths.engine_workdir()`.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


# ----------------------------------------------------------------- src on path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from autoapply_next.engine.persistence import (  # noqa: E402
    canonical_seek_url,
    queued_urls_for_batch,
)
from autoapply_next.platform.paths import engine_workdir  # noqa: E402


# ----------------------------------------------------------------- helpers


def resolve_workdir() -> Path:
    env = os.environ.get("AUTOAPPLY_NEXT_ENGINE_WORKDIR")
    if env:
        return Path(env).resolve()
    return Path(engine_workdir()).resolve()


def read_status(workdir: Path, url: str) -> tuple[str | None, str | None, int]:
    """Return (status, timestamp, failure_count) for a row, or (None, None, 0)
    if the row is missing. Opens the DB in read-only URI mode to be
    extra-explicit about not writing."""
    db_path = workdir / "jobs.db"
    if not db_path.exists():
        return None, None, 0
    uri = f"file:{db_path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                "SELECT status, timestamp, COALESCE(failure_count, 0) "
                "FROM applications WHERE url = ?",
                (url,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        print(f"[ERROR] DB read failed: {exc}", file=sys.stderr)
        return None, None, 0
    if row is None:
        return None, None, 0
    return row[0], row[1], int(row[2] or 0)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------- modes


def mode_watch(urls: list[str], workdir: Path, interval: float) -> int:
    """Poll every `interval` seconds; print baseline then any transitions.
    Returns 0 when the user Ctrl+Cs."""
    canonical = [canonical_seek_url(u) for u in urls]
    state: dict[str, tuple[str | None, str | None, int]] = {
        u: (None, None, 0) for u in canonical
    }
    print(f"[{now_str()}] watching {len(canonical)} url(s) at {workdir}/jobs.db")
    print(f"[{now_str()}] polling every {interval:.1f}s; Ctrl+C to stop")
    # Baseline.
    for u in canonical:
        s, t, fc = read_status(workdir, u)
        state[u] = (s, t, fc)
        print(f"[{now_str()}] BASELINE  status={s!r:25s} fc={fc}  {u}")
    try:
        while True:
            time.sleep(interval)
            for u in canonical:
                s, t, fc = read_status(workdir, u)
                old_s, old_t, old_fc = state[u]
                if s != old_s or fc != old_fc:
                    arrow = f"{old_s!r} -> {s!r}" if old_s != s else f"fc {old_fc} -> {fc}"
                    print(
                        f"[{now_str()}] TRANSITION {arrow}  "
                        f"(row ts={t})  {u}"
                    )
                    state[u] = (s, t, fc)
    except KeyboardInterrupt:
        print(f"\n[{now_str()}] stopped by user")
        return 0


def mode_check_eligibility(urls: list[str], workdir: Path) -> int:
    """One-shot cross-run-duplicate-guard confirmation.

    Calls the REAL persistence.queued_urls_for_batch (the same filter the
    batch prepare uses) with min_score=0 so nothing is excluded by score,
    and asserts that NONE of the user-supplied URLs appear in the
    eligible set. If any do, the cross-run guard is broken and the user
    should NOT run another batch.
    """
    canonical = [canonical_seek_url(u) for u in urls]
    print(f"[{now_str()}] checking eligibility against {workdir}/jobs.db")
    print(f"[{now_str()}] {len(canonical)} url(s) to check")

    eligible_set = set(
        queued_urls_for_batch(engine_workdir=workdir, min_score=0)
    )
    print(
        f"[{now_str()}] persistence.queued_urls_for_batch returned "
        f"{len(eligible_set)} eligible url(s) total"
    )
    leaks = [u for u in canonical if u in eligible_set]
    if leaks:
        print(f"[{now_str()}] FAIL: {len(leaks)} url(s) are STILL eligible:")
        for u in leaks:
            print(f"  - {u}")
        print(f"[{now_str()}] cross-run duplicate guard is broken; do NOT run another batch")
        return 2
    print(f"[{now_str()}] PASS: none of the {len(canonical)} url(s) are eligible")
    print(f"[{now_str()}] the next batch prepare will NOT include them; cross-run guard holds")
    return 0


# ----------------------------------------------------------------- cli


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="watch_live_batch.py",
        description=(
            "Read-only monitor for a live batch. Watches jobs.db status "
            "transitions and confirms the cross-run guard."
        ),
    )
    parser.add_argument(
        "urls", nargs="+",
        help="Seek job URLs to monitor (any canonical form accepted).",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Polling mode (default if neither --watch nor --check-eligibility is given).",
    )
    parser.add_argument(
        "--check-eligibility", action="store_true",
        help="One-shot eligibility check (PASS / FAIL) and exit.",
    )
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="Polling interval in seconds (default 2.0).",
    )
    parser.add_argument(
        "--workdir", type=Path, default=None,
        help="Override engine workdir (default: $AUTOAPPLY_NEXT_ENGINE_WORKDIR "
             "or the platform default).",
    )
    args = parser.parse_args()

    workdir = (args.workdir or resolve_workdir()).resolve()
    if not workdir.exists():
        print(f"[ERROR] engine workdir not found: {workdir}", file=sys.stderr)
        return 1
    if not (workdir / "jobs.db").exists():
        print(f"[ERROR] no jobs.db at {workdir}", file=sys.stderr)
        return 1

    if args.check_eligibility:
        return mode_check_eligibility(args.urls, workdir)
    # Default to watch.
    return mode_watch(args.urls, workdir, args.interval)


if __name__ == "__main__":
    sys.exit(main())
