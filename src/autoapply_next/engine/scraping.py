"""Scrape and score Seek listings for a keyword, persist into jobs.db.

Wraps the engine's `scraper.seek.SeekScraper` so the GUI can call one
function and get back a list of structured rows. We do exactly what the
engine's `main.py` daemon does in its scrape + pre-score pass, minus the
infinite loop: scrape one page per keyword, dedupe against `seen_jobs`,
fetch JD for each new job, score via `matcher.score_job`, persist into
`applications` with `status="queued"`.

Throttling: SeekScraper has internal `human_delay(2, 4)` between pages.
The engine's daemon caps at 17 pages per skill but for an interactive
Refresh we only fetch the first page so the user gets results in tens of
seconds, not minutes. The user can hit Refresh again for the next page.

Cancellation: between every per-job step, poll `is_cancelled`. Mid-Playwright
cancellation propagates via `asyncio.Task.cancel()` which the worker raises
when its cancel flag fires.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .adapter import _engine_workdir

logger = logging.getLogger(__name__)


# Cap each Refresh to a sane number of jobs to score so we don't lock the
# worker for many minutes. The user can scrape again for more.
MAX_NEW_JOBS_PER_REFRESH = 12


def _has_application_row(tracker_mod, url: str) -> bool:
    """Check if the engine's `applications` table already has a row for this
    URL. tracker.is_applied_or_skipped only matches status in ('applied',
    'skipped'), which would let 'queued', 'failed', 'in_progress' through.
    For dedupe in the interactive queue we want any existing row to count."""
    try:
        import sqlite3

        with sqlite3.connect(tracker_mod.DB_PATH) as conn:
            cur = conn.execute(
                "SELECT 1 FROM applications WHERE url=?", (url,)
            )
            return cur.fetchone() is not None
    except Exception:
        return False


@dataclass(frozen=True)
class ScrapedJob:
    url: str
    title: str
    company: str
    score: int | None
    reasoning: str | None
    description_chars: int


@dataclass(frozen=True)
class ScrapeResult:
    keyword: str
    total_scraped: int
    new_jobs: int
    scored: list[ScrapedJob]
    errors: list[str]


StatusCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]


async def scrape_and_score(
    *,
    keyword: str,
    engine_workdir: Path,
    location: str = "Australia",
    on_status: StatusCallback | None = None,
    is_cancelled: CancelCheck | None = None,
    max_jobs: int = MAX_NEW_JOBS_PER_REFRESH,
) -> ScrapeResult:
    """Scrape Seek for one keyword and score each new listing.

    Returns a `ScrapeResult` with the list of jobs persisted to `jobs.db`
    with `status="queued"`. Existing rows in `applications` for the same
    URL are not touched.
    """
    on_status = on_status or (lambda _msg: None)
    is_cancelled = is_cancelled or (lambda: False)

    engine_workdir = Path(engine_workdir).resolve()
    errors: list[str] = []

    with _engine_workdir(engine_workdir):
        # Lazy imports under cwd context so engine module-level paths resolve.
        try:
            from playwright.async_api import async_playwright  # type: ignore[import-not-found]

            import matcher  # type: ignore[import-not-found]
            import seek_apply  # type: ignore[import-not-found]
            import tracker  # type: ignore[import-not-found]
            from models import Application  # type: ignore[import-not-found]
            from scraper.seek import SeekScraper  # type: ignore[import-not-found]
        except Exception as exc:
            logger.exception("scrape_and_score: imports failed")
            errors.append(f"engine import failed: {exc}")
            return ScrapeResult(
                keyword=keyword,
                total_scraped=0,
                new_jobs=0,
                scored=[],
                errors=errors,
            )

        on_status(f"Scraping Seek for '{keyword}'...")
        scraper = SeekScraper()
        try:
            async with async_playwright() as pw:
                await scraper.start(pw)
                try:
                    if is_cancelled():
                        raise asyncio.CancelledError()
                    # Limit to the first page worth of jobs by passing one
                    # skill and letting SeekScraper.scrape do its thing.
                    # SeekScraper internally caps at 17 pages but the empty
                    # consecutive-pages bailout usually stops at 2 to 3 pages
                    # for niche keywords. We additionally cap below via
                    # `max_jobs` after dedupe.
                    raw = await scraper.scrape([keyword], location=location)
                finally:
                    try:
                        await scraper.stop()
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("scrape_and_score: SeekScraper failed")
            errors.append(f"scrape failed: {exc}")
            return ScrapeResult(
                keyword=keyword,
                total_scraped=0,
                new_jobs=0,
                scored=[],
                errors=errors,
            )

        on_status(f"Scraped {len(raw)} jobs; deduping against jobs.db")

        # Dedupe rule: skip only URLs that are already in `applications`
        # (regardless of status). Previously-`seen` URLs that never made it
        # into `applications` (e.g. the engine's daemon scraped them but
        # never scored them) ARE re-scored here, because the user might want
        # to revisit them now. The engine's daemon dedupes more aggressively
        # against `seen_jobs` for efficiency; that is correct for an
        # unattended loop but wrong for interactive review.
        new_listings = []
        already_in_queue = set()
        try:
            for j in raw:
                if is_cancelled():
                    raise asyncio.CancelledError()
                if _has_application_row(tracker, j.url):
                    already_in_queue.add(j.url)
                    continue
                new_listings.append(j)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("scrape_and_score: dedupe failed: %s", exc)
            errors.append(f"dedupe failed: {exc}")
            new_listings = raw
        # Alias so downstream messaging reads correctly.
        already_applied = already_in_queue

        # Cap before scoring (each score is a Claude round-trip).
        capped = new_listings[:max_jobs]
        if len(new_listings) > max_jobs:
            on_status(
                f"{len(new_listings)} new jobs; scoring the first {max_jobs}. "
                "Hit Refresh again for the next batch."
            )
        else:
            on_status(f"{len(new_listings)} new jobs; scoring all of them.")

        scored: list[ScrapedJob] = []
        for i, job in enumerate(capped, 1):
            if is_cancelled():
                raise asyncio.CancelledError()
            on_status(f"[{i}/{len(capped)}] fetching JD: {job.title}")
            try:
                # Fetch the full JD so the matcher has something to chew on.
                # Listing scrape only gives us title+company+posted; we need
                # description for a meaningful score.
                session_state = str(
                    (engine_workdir / "sessions" / "seek" / "state.json").resolve()
                )
                description = await seek_apply.fetch_seek_jd(
                    job.url, session_state
                )
                job.description = description
            except Exception as exc:
                logger.warning("scrape_and_score: JD fetch failed for %s: %s", job.url, exc)
                errors.append(f"jd fetch failed ({job.url}): {exc}")
                job.description = ""

            on_status(f"[{i}/{len(capped)}] scoring: {job.title}")
            try:
                score, reasoning = await matcher.score_job(job)
            except Exception as exc:
                logger.warning("scrape_and_score: score failed for %s: %s", job.url, exc)
                errors.append(f"score failed ({job.url}): {exc}")
                score, reasoning = None, str(exc)

            # Persist as a queued application. tracker.upsert_application uses
            # INSERT OR REPLACE so a subsequent apply transitions the status
            # cleanly.
            try:
                tracker.mark_seen(job.url)
                tracker.upsert_application(
                    Application(
                        url=job.url,
                        title=job.title or "Seek listing",
                        company=job.company or "",
                        board="seek",
                        match_score=int(score or 0),
                        match_reasoning=reasoning or "",
                        status="queued",
                    )
                )
            except Exception as exc:
                logger.warning(
                    "scrape_and_score: persist failed for %s: %s", job.url, exc
                )
                errors.append(f"persist failed ({job.url}): {exc}")

            scored.append(
                ScrapedJob(
                    url=job.url,
                    title=job.title or "Seek listing",
                    company=job.company or "",
                    score=score,
                    reasoning=reasoning,
                    description_chars=len(job.description or ""),
                )
            )

        # Close engine's _PeekSession too: fetch_seek_jd uses it under the
        # hood and leaves a global context alive across runs. Closing here
        # avoids a stale browser when the user invokes apply right after.
        try:
            await seek_apply._PeekSession.close()
        except Exception:
            pass

        on_status(
            f"Done. {len(scored)} scored, {len(already_applied)} already seen/applied."
        )
        return ScrapeResult(
            keyword=keyword,
            total_scraped=len(raw),
            new_jobs=len(new_listings),
            scored=scored,
            errors=errors,
        )
