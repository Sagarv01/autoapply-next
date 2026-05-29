"""
End-to-end test: fetch description → tailor → apply for a single Seek job.
Run with: python test_seek_apply.py
"""
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_seek_apply")

TARGET_URL = "https://au.seek.com/job/91283052"
JOB_TITLE = "Platform & Automation Architect"
JOB_COMPANY = "Interactive Pty Ltd"


async def fetch_description(url: str) -> str:
    """Open the job page and extract the description text."""
    from scraper import BaseScraper

    class _TempScraper(BaseScraper):
        board = "seek"
        async def scrape(self, skills): return []

    scraper = _TempScraper()
    async with async_playwright() as pw:
        await scraper.start(pw)
        page = await scraper._context.new_page()
        try:
            logger.info(f"Fetching job page: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            el = await page.query_selector('[data-automation="jobAdDetails"]')
            if not el:
                logger.warning("jobAdDetails element not found — page may have changed")
                return ""
            text = (await el.inner_text()).strip()
            logger.info(f"Fetched description ({len(text)} chars)")
            return text
        finally:
            await page.close()
            await scraper.stop()


async def main():
    from models import JobListing
    import yaml

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    candidate = cfg["candidate"]

    # Step 1: fetch description
    logger.info("=== STEP 1: Fetch job description ===")
    description = await fetch_description(TARGET_URL)
    if not description:
        logger.error("Could not fetch description — aborting.")
        sys.exit(1)

    job = JobListing(
        url=TARGET_URL,
        title=JOB_TITLE,
        company=JOB_COMPANY,
        board="seek",
        description=description,
        posted_at="",
    )
    logger.info(f"Job: {job.title} @ {job.company}")

    # Step 2: tailor resume + cover letter
    logger.info("=== STEP 2: Tailor resume & cover letter ===")
    from tailorer import tailor
    try:
        resume_pdf, cover_pdf = await tailor(job)
        logger.info(f"Resume:      {resume_pdf}")
        logger.info(f"Cover letter: {cover_pdf}")
    except Exception as e:
        logger.error(f"Tailor failed: {e}", exc_info=True)
        sys.exit(1)

    # Step 3: apply
    logger.info("=== STEP 3: Submit application via browser-use ===")
    from applicator import apply, BoardBlockedError
    try:
        result = await apply(job, resume_pdf, cover_pdf, candidate)
        logger.info(f"Apply result: {result}")
        logger.info("SUCCESS — application submitted.")
    except BoardBlockedError as e:
        logger.error(f"BLOCKED: {e}")
        sys.exit(1)
    except TimeoutError as e:
        logger.error(f"TIMEOUT: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"FAILED: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
