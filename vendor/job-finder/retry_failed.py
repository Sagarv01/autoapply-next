"""Retry specific failed Seek jobs by URL through the same production pipeline.

Run: venv/bin/python retry_failed.py <url> [<url> ...]

For each URL:
  1. Resets the application row from 'failed' back to 'queued' so the pipeline
     will re-process it (otherwise tracker.is_applied_or_skipped short-circuits).
  2. peek_is_quick_apply → fetch_seek_jd → score_job → tailor → apply.
  3. Prints per-job outcome (applied / skipped / failed-with-reason).

Use this AFTER fixing a bug to confirm the retry now succeeds.
"""
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

import tracker
from applicator import BoardBlockedError, apply
from matcher import score_job
from models import Application, JobListing
from seek_apply import (
    ExternalApplyError,
    _PeekSession,
    fetch_seek_jd,
    peek_is_quick_apply,
)
from tailorer import CoverLetterQualityError, tailor

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("retry")

SESSION_STATE = str(Path("sessions/seek/state.json").resolve())


def _reset_to_queued(url: str) -> tuple[str, str] | None:
    """Flip the row back to queued so the pipeline will run; return (title, company)."""
    conn = sqlite3.connect(tracker.DB_PATH)
    row = conn.execute(
        "SELECT title, company, board FROM applications WHERE url=?",
        (url,),
    ).fetchone()
    if row is None:
        conn.close()
        log.error(f"no tracker row for {url}")
        return None
    title, company, _board = row
    conn.execute(
        "UPDATE applications SET status='queued', notes='', failure_count=0 WHERE url=?",
        (url,),
    )
    conn.commit()
    conn.close()
    log.info(f"reset to queued: {title} @ {company}")
    return title, company


async def _retry_one(url: str, candidate: dict) -> str:
    info = _reset_to_queued(url)
    if info is None:
        return "no_row"
    title, company = info

    job = JobListing(url=url, title=title, company=company, board="seek",
                     description="", easy_apply=True)
    app = Application(url=url, title=title, company=company, board="seek")

    try:
        is_quick, open_page = await peek_is_quick_apply(url, SESSION_STATE)
        if not is_quick:
            app.status = "skipped"
            app.notes = "External apply (re-check)"
            log.info(f"  ⊘ external")
            tracker.upsert_application(app)
            return "skipped_external"

        jd = await fetch_seek_jd(url, SESSION_STATE)
        if jd:
            job.description = jd
            log.info(f"  jd: {len(jd)} chars")

        score, reasoning = await score_job(job)
        app.match_score = score
        app.match_reasoning = reasoning
        log.info(f"  score: {score}%")

        app.status = "in_progress"
        tracker.upsert_application(app)

        resume_pdf, cover_pdf = await tailor(job, tier="full" if score >= 50 else ("quick" if score >= 20 else "base"))
        app.resume_file = Path(resume_pdf).name
        app.cover_letter_file = Path(cover_pdf).name

        await apply(job, resume_pdf, cover_pdf, candidate, page=open_page)
        app.status = "applied"
        app.notes = "Retried via retry_failed.py"
        tracker.upsert_application(app)
        log.info(f"  ✅ APPLIED")
        return "applied"

    except CoverLetterQualityError as e:
        app.status = "skipped"
        app.notes = f"COVER_LETTER_QUALITY_FAIL marker={e.reason} jd_len={e.jd_len}"
        tracker.upsert_application(app)
        log.error(f"  ✗ quality gate: {e.reason}")
        return f"quality_gate:{e.reason}"
    except ExternalApplyError as e:
        app.status = "skipped"
        app.notes = str(e)
        tracker.upsert_application(app)
        log.info(f"  ⊘ external (mid-apply)")
        return "skipped_external_mid"
    except (BoardBlockedError, TimeoutError) as e:
        app.status = "failed"
        app.notes = str(e)
        tracker.upsert_application(app)
        log.error(f"  ✗ {type(e).__name__}: {str(e)[:120]}")
        return f"failed:{type(e).__name__}"
    except Exception as e:
        app.status = "failed"
        app.notes = str(e)
        tracker.upsert_application(app)
        log.error(f"  ✗ unexpected: {type(e).__name__}: {str(e)[:120]}")
        return f"failed:{type(e).__name__}"


async def main():
    if len(sys.argv) < 2:
        print("usage: retry_failed.py <url> [<url> ...]", file=sys.stderr)
        sys.exit(2)
    urls = sys.argv[1:]

    import yaml
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    candidate = cfg["candidate"]

    results: dict[str, str] = {}
    for url in urls:
        log.info(f"\n=== {url} ===")
        try:
            results[url] = await _retry_one(url, candidate)
        except Exception as e:
            log.exception(f"retry_failed top-level error for {url}: {e}")
            results[url] = f"crashed:{type(e).__name__}"

    await _PeekSession.close()

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    for url, outcome in results.items():
        print(f"  {outcome:30s}  {url}")


if __name__ == "__main__":
    asyncio.run(main())
