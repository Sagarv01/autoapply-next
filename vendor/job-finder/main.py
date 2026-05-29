import argparse
import asyncio
import logging
import os
import random
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright

import tracker
from applicator import BoardBlockedError, apply
from process_lock import seek_lock
from seek_apply import ExternalApplyError, fetch_seek_jd, peek_is_quick_apply
from matcher import score_job
from models import Application, JobListing
from scraper.seek import SeekScraper
from tailorer import CoverLetterQualityError, tailor
from utils import detect_libreoffice

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────
# force=True clears any handlers added by imported libraries (e.g. browser_use)
# before our RotatingFileHandler gets a chance to be registered.
handler = RotatingFileHandler("bot.log", maxBytes=10 * 1024 * 1024, backupCount=5)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[handler, logging.StreamHandler()],
    force=True,
)
logger = logging.getLogger("main")


def load_config() -> dict:
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def init_directories():
    for d in ["output", "errors", "sessions/linkedin", "sessions/seek", "sessions/indeed"]:
        Path(d).mkdir(parents=True, exist_ok=True)


def validate_env():
    # LLM calls go through the `claude` CLI; only SEEK_EMAIL is required.
    required = ["SEEK_EMAIL"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"Missing .env keys: {', '.join(missing)}")


def recover_orphans() -> list[JobListing]:
    orphans = tracker.get_orphaned_applications()
    permafail = tracker.permanently_failed_urls()
    requeued = []
    for app in orphans:
        if app.url in permafail:
            # Already past the cumulative-failure threshold. Don't waste
            # another retry cycle on a structurally-broken application.
            app.status = "failed"
            app.notes = (
                f"Auto-skipped: {app.failure_count} prior failures "
                f"(>= {tracker.PERMANENT_FAILURE_THRESHOLD}). "
                f"Last note: {app.notes[:120]}"
            )
            tracker.upsert_application(app)
            logger.info(
                f"Orphan skipped (perma-failed {app.failure_count}x): "
                f"{app.title} @ {app.company}"
            )
            continue
        if app.status == "in_progress":
            # Was mid-apply when bot stopped — mark failed and retry
            app.status = "failed"
            app.notes = "Orphaned — daemon restarted mid-apply"
            tracker.upsert_application(app)
            logger.warning(f"Orphaned in_progress re-queued: {app.title} @ {app.company}")
        else:
            # Was queued but never processed — just re-queue silently
            logger.info(f"Resuming queued job: {app.title} @ {app.company}")
        requeued.append(JobListing(
            url=app.url, title=app.title, company=app.company,
            board=app.board, description="",
            easy_apply=(app.board == "seek"),  # Seek jobs always use Quick Apply path
        ))
    return requeued


async def _process_job(job: JobListing, cfg: dict, state: dict):
    """Process a single job end-to-end: peek → score → tailor → apply → track."""
    candidate = cfg["candidate"]
    app = Application(url=job.url, title=job.title, company=job.company, board=job.board)
    try:
        if job.board == "seek":
            session_state = str(Path("sessions/seek/state.json").resolve())
            is_quick, open_page = await peek_is_quick_apply(job.url, session_state)
            if not is_quick:
                app.status = "skipped"
                app.notes = "External apply — not Quick Apply"
                logger.info(f"Skipped (external): {job.title} @ {job.company}")
            else:
                if not job.description:
                    fetched_description = await fetch_seek_jd(job.url, session_state)
                    if fetched_description:
                        job.description = fetched_description
                        logger.info(f"Fetched description ({len(fetched_description)} chars): {job.title} @ {job.company}")

                # Reuse pre-score from Phase 1.5 if available, else score now.
                existing = tracker.get_application_by_url(job.url)
                if existing and existing.match_score is not None:
                    score, reasoning = existing.match_score, existing.match_reasoning or ""
                else:
                    score, reasoning = await score_job(job)
                app.match_score = score
                app.match_reasoning = reasoning

                # Pick tailoring tier based on match score.
                tier = "full" if score >= 50 else ("quick" if score >= 20 else "base")
                logger.info(f"Tailoring tier={tier} ({score}%): {job.title} @ {job.company}")
                app.status = "in_progress"
                tracker.upsert_application(app)

                # Cache check: reuse prior tailored PDFs if they exist on disk.
                resume_pdf = cover_pdf = None
                if existing and existing.resume_file and existing.cover_letter_file:
                    rp = Path("output") / existing.resume_file
                    cp = Path("output") / existing.cover_letter_file
                    if rp.exists() and cp.exists():
                        resume_pdf, cover_pdf = str(rp), str(cp)
                        logger.info(f"Reusing cached PDFs (skip Claude tailor): {existing.resume_file}")
                if resume_pdf is None:
                    resume_pdf, cover_pdf = await tailor(job, tier=tier)
                app.resume_file = Path(resume_pdf).name
                app.cover_letter_file = Path(cover_pdf).name
                await apply(job, resume_pdf, cover_pdf, candidate, page=open_page)
                app.status = "applied"
                app.notes = "Cover letter uploaded" if cover_pdf else ""
                logger.info(f"Applied: {job.title} @ {job.company} ({job.board})")

    except CoverLetterQualityError as e:
        app.status = "skipped"
        app.notes = (
            f"COVER_LETTER_QUALITY_FAIL marker={e.reason} jd_len={e.jd_len} "
            f"preview={e.output_preview[:160]!r}"
        )
        logger.error(
            f"Skipped (cover letter quality gate: {e.reason}): {job.title} @ {job.company}"
        )

    except ExternalApplyError as e:
        app.status = "skipped"
        app.notes = str(e)
        logger.info(f"Skipped (external apply): {job.title} @ {job.company}")

    except BoardBlockedError as e:
        app.status = "failed"
        app.notes = str(e)
        msg = str(e)
        if "session expired" in msg.lower():
            logger.critical("Seek session expired. Re-run setup_sessions.py to refresh login.")
        else:
            logger.warning(f"Board blocked: {e}")

    except TimeoutError as e:
        app.status = "failed"
        app.notes = str(e)
        logger.error(f"Timeout: {e}")

    except Exception as e:
        app.status = "failed"
        app.notes = str(e)
        logger.error(f"Application failed: {job.url} — {e}")

    finally:
        tracker.upsert_application(app)
        state["last_job_ts"] = time.time()

    return app.status, app.notes


MAX_RETRIES = 3
MIN_SCORE_TO_APPLY = 20  # skip any job below this match score
MAX_APPLIES_PER_RUN = 100  # cap on successful applies per scrape cycle
RETRY_DELAY = 30  # seconds between attempts

# Throughput cap: ~30-40 successful applies / hour (avg 90s between submits).
# Sleep ONLY after successful submits, not after skipped/failed jobs.
APPLY_GAP_MIN = 60   # seconds
APPLY_GAP_MAX = 120  # seconds


async def _apply_with_retry(job: JobListing, cfg: dict, state: dict) -> str:
    """Apply to a job, retrying on failure. Returns final status."""
    for attempt in range(1, MAX_RETRIES + 1):
        status, notes = await _process_job(job, cfg, state)

        if status in ("applied", "skipped"):
            return status

        # status == "failed" — decide whether to retry
        if "session expired" in notes.lower():
            logger.error(f"Session expired — cannot retry {job.title}. Re-run setup_sessions.py.")
            return status

        if attempt < MAX_RETRIES:
            wait = RETRY_DELAY * attempt
            logger.warning(
                f"Attempt {attempt}/{MAX_RETRIES} failed for '{job.title}' @ {job.company} "
                f"({notes[:80]}) — retrying in {wait}s"
            )
            await asyncio.sleep(wait)
        else:
            logger.error(
                f"All {MAX_RETRIES} attempts failed for '{job.title}' @ {job.company}. "
                f"URL={job.url} last_error={notes[:120]}"
            )
    return "failed"


async def watchdog(state: dict):
    """Runs every hour. Auto-fixes stuck in_progress jobs and logs a health summary."""
    STUCK_JOB_MINUTES = 15

    while True:
        await asyncio.sleep(3600)
        stuck = tracker.get_stuck_in_progress(minutes=STUCK_JOB_MINUTES)
        for app in stuck:
            app.status = "failed"
            app.notes = f"Watchdog: stuck in_progress > {STUCK_JOB_MINUTES}min, force-failed"
            tracker.upsert_application(app)
            logger.warning(f"Watchdog force-failed stuck job: {app.title} @ {app.company}")

        stats = tracker.get_stats()
        logger.info(
            f"Watchdog hourly — applied={stats.get('applied', 0)} "
            f"skipped={stats.get('skipped', 0)} failed={stats.get('failed', 0)} "
            f"idle={(time.time() - state['last_job_ts']) / 60:.0f}min"
        )
        if stuck:
            logger.warning(
                f"Watchdog auto-fixed {len(stuck)} stuck jobs: "
                + ", ".join(f"{a.title} @ {a.company}" for a in stuck[:5])
            )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["date", "relevance"], default="date",
        help="Starting sort: date = newest first; relevance = Seek's default. "
             "When a scrape cycle yields no new jobs, the bot auto-flips to "
             "the OTHER mode for the next cycle and keeps alternating."
    )
    parser.add_argument(
        "--nonstop", action="store_true",
        help="Apply back-to-back with no rate limit. Default mode keeps a 60-120s "
             "randomized delay after each successful apply (safer — lower risk of "
             "Seek's anti-automation flag tripping)."
    )
    args = parser.parse_args()
    sort_by_date = args.mode == "date"
    rate_limited = not args.nonstop

    load_dotenv()
    validate_env()
    init_directories()
    detect_libreoffice()  # fail fast if missing
    tracker.init_db()

    cfg = load_config()
    skills = cfg["search"]["skills"]
    location = cfg["search"]["location"]
    interval = cfg["scraper"]["interval_seconds"]
    jitter = cfg["scraper"]["interval_jitter_seconds"]

    state: dict = {"last_job_ts": time.time()}

    rate_msg = f"rate-limited {APPLY_GAP_MIN}-{APPLY_GAP_MAX}s/apply" if rate_limited else "NONSTOP (no per-apply delay)"
    logger.info(f"Job bot starting — mode={args.mode}, {rate_msg}")

    # Watchdog runs independently in the background (hourly, non-blocking)
    asyncio.create_task(watchdog(state))

    while True:
        # ── Phase 0: Finish any queued jobs before scraping new ones ─────
        queued = recover_orphans()
        if queued:
            logger.info(f"Processing {len(queued)} queued jobs before scraping...")
            for job in queued:
                status = await _apply_with_retry(job, cfg, state)
                if status == "applied" and rate_limited:
                    gap = random.uniform(APPLY_GAP_MIN, APPLY_GAP_MAX)
                    logger.info(f"Rate limit: sleeping {gap:.0f}s before next apply")
                    await asyncio.sleep(gap)
            # Phase 0's _PeekSession holds seek_chrome_profile — close it before
            # Phase 1's scraper tries to open the same user-data-dir.
            from seek_apply import _PeekSession
            await _PeekSession.close()

        # ── Phase 1: Scrape ───────────────────────────────────────────────
        jobs: list[JobListing] = []
        scraper = SeekScraper()
        try:
            async with async_playwright() as pw:
                await scraper.start(pw)
                jobs = await scraper.scrape(skills, location,
                                              sort_by_date=sort_by_date,
                                              max_jobs=MAX_APPLIES_PER_RUN)
        except PermissionError as e:
            logger.critical(f"Seek login failed: {e}")
        except Exception as e:
            logger.error(f"Scrape error: {e}")

        # ── Phase 1.5: Pre-score every scraped job and sort by match desc ──
        # So the highest-match jobs apply FIRST. If Seek throttles us mid-batch,
        # we've already submitted the most relevant ones. External-apply jobs
        # are deprioritized to the bottom (we can't apply to them anyway).
        if jobs:
            session_state = str(Path("sessions/seek/state.json").resolve())
            logger.info(f"Pre-scoring {len(jobs)} scraped jobs (peek + GPT-4o-mini) — this takes time...")
            scored: list[tuple[int, JobListing]] = []
            reasoning_by_url: dict[str, str] = {}
            for i, job in enumerate(jobs, 1):
                try:
                    is_quick, peek_page = await peek_is_quick_apply(job.url, session_state)
                    if peek_page:
                        try:
                            await peek_page.close()
                        except Exception:
                            pass
                    if not is_quick:
                        # External-apply: bot can't fill these. Drop entirely
                        # (already marked seen by scraper) and record as skipped.
                        app = Application(
                            url=job.url, title=job.title, company=job.company, board=job.board,
                            status="skipped", notes="External apply (detected at pre-score)",
                        )
                        tracker.upsert_application(app)
                        logger.info(f"  [{i}/{len(jobs)}] external (dropped): {job.title}")
                        continue
                    jd = await fetch_seek_jd(job.url, session_state)
                    if jd:
                        job.description = jd
                    score, reasoning = await score_job(job)
                    scored.append((score, job))
                    reasoning_by_url[job.url] = reasoning
                    logger.info(f"  [{i}/{len(jobs)}] score={score}% — {job.title}")
                except Exception as e:
                    scored.append((0, job))
                    reasoning_by_url[job.url] = f"score failed: {type(e).__name__}: {str(e)[:120]}"
                    logger.warning(f"  [{i}/{len(jobs)}] score failed: {e} — {job.title}")

            scored.sort(key=lambda x: -x[0])
            # Drop jobs scoring below MIN_SCORE_TO_APPLY (don't even queue them)
            before_filter = len(scored)
            scored = [(s, j) for s, j in scored if s >= MIN_SCORE_TO_APPLY]
            # Cap at MAX_APPLIES_PER_RUN highest-scored jobs
            scored = scored[:MAX_APPLIES_PER_RUN]
            jobs = [j for _, j in scored]
            logger.info(
                f"Filtered: {before_filter} scored → {len(jobs)} kept "
                f"(score ≥ {MIN_SCORE_TO_APPLY}%, capped at {MAX_APPLIES_PER_RUN})"
            )
            top5 = ", ".join(f"{s}% {j.title[:30]}" for s, j in scored[:5])
            logger.info(f"Top 5: {top5}")

            score_by_url = {j.url: s for s, j in scored}
            for job in jobs:
                tracker.save_queued_job(
                    job.url, job.title, job.company, job.board,
                    match_score=score_by_url.get(job.url),
                    match_reasoning=reasoning_by_url.get(job.url, ""),
                )
                logger.info(f"Queued: {job.title} @ {job.company}")

            # Close the peek browser before Phase 2 starts (avoids SingletonLock)
            from seek_apply import _PeekSession
            await _PeekSession.close()

        # ── Phase 2: Apply each job one by one (with retry) ──────────────
        logger.info(f"Scrape complete — {len(jobs)} new jobs to process")
        for job in jobs:
            status = await _apply_with_retry(job, cfg, state)
            if status == "applied" and rate_limited:
                gap = random.uniform(APPLY_GAP_MIN, APPLY_GAP_MAX)
                logger.info(f"Rate limit: sleeping {gap:.0f}s before next apply")
                await asyncio.sleep(gap)

        # Close the apply browser before the next scrape, otherwise its
        # SingletonLock on seek_chrome_profile/ blocks the next scraper launch.
        from seek_apply import _PeekSession
        await _PeekSession.close()

        # ── Mode auto-flip ────────────────────────────────────────────────
        # If the current mode's top-17 pages are exhausted (every URL
        # already-seen / applied / skipped / permafail), switch sort for
        # the next cycle. This pulls in jobs the OTHER ranking surfaces
        # (date-sort sees latest by post time; relevance-sort weights
        # older but better-matching listings). Alternates indefinitely.
        if not jobs and not queued:
            sort_by_date = not sort_by_date
            new_mode = "date" if sort_by_date else "relevance"
            logger.info(f"No new jobs this cycle — flipping sort to {new_mode}")

        # ── Phase 3: Sleep ────────────────────────────────────────────────
        sleep_s = max(interval + random.randint(-jitter, jitter), 60)
        logger.info(f"All done. Sleeping {sleep_s}s before next scrape cycle...")
        await asyncio.sleep(sleep_s)


if __name__ == "__main__":
    # Keep the Mac awake while the bot runs (idle sleep + display sleep).
    # `caffeinate` is auto-killed when this process exits.
    import shutil, subprocess, atexit
    if shutil.which("caffeinate"):
        _caf = subprocess.Popen(["caffeinate", "-disu"])
        atexit.register(lambda: _caf.terminate())
        logger.info(f"Started caffeinate (pid={_caf.pid}) — Mac will not sleep while bot runs")

    # Refuse to start if another Seek session (audit/diagnostic/another bot)
    # is active — parallel browsers tripped Seek's anti-automation today.
    with seek_lock():
        asyncio.run(main())
