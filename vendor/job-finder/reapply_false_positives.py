"""
Re-apply to jobs that are marked 'applied' in our DB but are NOT actually on
Seek's Applied Jobs page (false positives from the broken legacy verifier).

Reads errors/audit/false_positives.json (produced by audit_applied.py).

For each entry:
  1. Skip if no longer applyable (expired listing or already applied since audit)
  2. Otherwise run the same pipeline main.py uses (peek → score → tailor → apply)
  3. The patched verifier in seek_apply.py now does title+company match on the
     real /my-activity/applied-jobs URL — so a successful return means it's
     genuinely on Seek's list.

Pass --apply to actually run; default is a dry-run that just prints what would
be processed.
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright

import tracker
from applicator import BoardBlockedError
from seek_apply import ExternalApplyError, fetch_seek_jd, peek_is_quick_apply
from matcher import score_job
from models import Application, JobListing
from tailorer import tailor

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("reapply")

FP_FILE = Path("errors/audit/false_positives.json")
SESSION = str(Path("sessions/seek/state.json").resolve())
DRY_RUN = "--apply" not in sys.argv
LIMIT = int(os.environ.get("REAPPLY_LIMIT", "200"))


async def main():
    if not FP_FILE.exists():
        log.error(f"{FP_FILE} not found — run audit_applied.py first.")
        return

    entries = json.loads(FP_FILE.read_text())
    log.info(f"Loaded {len(entries)} false-positive entries from {FP_FILE}")

    # Skip entries that were already re-applied in a prior reapply session.
    # We CAN'T trust status='applied' alone — most of these URLs lie about that
    # (broken legacy verifier). Use the re-apply note marker instead.
    import sqlite3
    conn0 = sqlite3.connect("jobs.db")
    reapplied_urls = {
        r[0] for r in conn0.execute(
            "SELECT url FROM applications WHERE status='applied' AND notes LIKE '%Re-applied via reapply_false_positives.py%'"
        ).fetchall()
    }
    conn0.close()
    before = len(entries)
    entries = [e for e in entries if e["url"] not in reapplied_urls]
    log.info(f"Filtered out {before - len(entries)} already re-applied URLs; {len(entries)} remain")
    log.info(f"Mode: {'DRY-RUN (no real applications)' if DRY_RUN else 'LIVE (will submit applications)'}")
    log.info(f"Limit: {LIMIT}")

    cfg = yaml.safe_load(open("config.yaml"))
    candidate = cfg["candidate"]

    tracker.init_db()

    summary = {"applied": 0, "skipped_external": 0, "skipped_already": 0, "failed": 0, "low_match": 0}
    skipped_titles = []

    async with async_playwright() as pw:
        for i, entry in enumerate(entries[:LIMIT], 1):
            url, title, company = entry["url"], entry["title"], entry["company"]
            log.info(f"\n[{i}/{min(len(entries), LIMIT)}] {title} @ {company}")

            if DRY_RUN:
                log.info("  (dry-run) would peek + tailor + apply")
                continue

            job = JobListing(url=url, title=title, company=company, board="seek",
                             description="", easy_apply=True)
            app = Application(url=url, title=title, company=company, board="seek")
            try:
                is_quick, open_page = await peek_is_quick_apply(url, SESSION)
                if not is_quick:
                    app.status = "skipped"
                    app.notes = "External apply (re-check) — bot cannot apply"
                    log.info(f"  ⊘ external apply — skipping")
                    summary["skipped_external"] += 1
                    skipped_titles.append(f"{title} @ {company} (external)")
                    continue

                jd = await fetch_seek_jd(url, SESSION)
                if jd:
                    job.description = jd
                score, reasoning = await score_job(job)
                app.match_score = score
                app.match_reasoning = reasoning
                log.info(f"  → tailoring at {score}% match (no score gate)")
                app.status = "in_progress"
                tracker.upsert_application(app)
                resume_pdf, cover_pdf = await tailor(job)
                app.resume_file = Path(resume_pdf).name
                app.cover_letter_file = Path(cover_pdf).name

                from applicator import apply
                await apply(job, resume_pdf, cover_pdf, candidate, page=open_page)
                app.status = "applied"
                app.notes = "Re-applied via reapply_false_positives.py"
                log.info(f"  ✅ applied: {title} @ {company}")
                summary["applied"] += 1

            except ExternalApplyError as e:
                app.status = "skipped"
                app.notes = str(e)
                summary["skipped_external"] += 1
                log.info(f"  ⊘ external apply: {e}")
            except BoardBlockedError as e:
                app.status = "failed"
                app.notes = str(e)
                summary["failed"] += 1
                log.error(f"  ❌ board blocked: {e}")
                if "session expired" in str(e).lower():
                    log.critical("Session expired — aborting.")
                    break
            except Exception as e:
                app.status = "failed"
                app.notes = str(e)
                summary["failed"] += 1
                log.error(f"  ❌ failed: {e}")
            finally:
                tracker.upsert_application(app)

    log.info("\n=========== RE-APPLY SUMMARY ===========")
    for k, v in summary.items():
        log.info(f"  {k}: {v}")
    log.info("=========================================")
    if skipped_titles:
        log.info("Skipped (sample):")
        for t in skipped_titles[:10]:
            log.info(f"  - {t}")


if __name__ == "__main__":
    asyncio.run(main())
