"""
Controlled test: scrape AWS Quick Apply jobs from Seek, apply to first 2.
Run: venv/bin/python test_2_apply.py
"""
import asyncio
import logging
import os
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from pathlib import Path

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

SEARCH_URL = "https://au.seek.com/jobs"
SESSION = Path("sessions/seek/state.json")


async def scan_quick_apply(max_pages: int = 5) -> list[dict]:
    """Scan Seek AWS jobs and find Quick Apply ones by checking each job page."""
    ctx_opts = {}
    if SESSION.exists():
        ctx_opts["storage_state"] = str(SESSION)
        logger.info(f"Using session: {SESSION}")
    else:
        logger.warning("No session — scraping without login (Quick Apply buttons may be hidden)")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(**ctx_opts)
        quick_apply_jobs = []

        for page_num in range(1, max_pages + 1):
            list_page = await ctx.new_page()
            try:
                params = f"?keywords=AWS&where=Australia&sortmode=ListedDate&page={page_num}"
                await list_page.goto(SEARCH_URL + params, wait_until="domcontentloaded")
                await asyncio.sleep(3)

                cards = await list_page.query_selector_all('[data-automation="normalJob"]')
                if not cards:
                    logger.info(f"Page {page_num}: no cards, stopping")
                    break

                logger.info(f"Page {page_num}: {len(cards)} cards")

                page_jobs = []
                for card in cards:
                    try:
                        link = await card.query_selector('a[data-automation="jobTitle"]')
                        company_el = await card.query_selector('[data-automation="jobCompany"]')
                        date_el = await card.query_selector('[data-automation="jobListingDate"]')
                        if not link:
                            continue
                        href = await link.get_attribute("href")
                        url = f"https://au.seek.com{href}".split("?")[0]
                        title = (await link.inner_text()).strip()
                        company = (await company_el.inner_text()).strip() if company_el else ""
                        posted = (await date_el.inner_text()).strip() if date_el else ""
                        page_jobs.append({"url": url, "title": title, "company": company, "posted_at": posted})
                    except Exception as e:
                        logger.warning(f"Card parse error: {e}")
            finally:
                await list_page.close()

            # Check each job page for Quick Apply button + fetch description
            for j in page_jobs:
                job_page = await ctx.new_page()
                try:
                    await job_page.goto(j["url"], wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)

                    apply_btn = await job_page.query_selector('[data-automation="job-detail-apply"]')
                    btn_text = (await apply_btn.inner_text()).strip().lower() if apply_btn else ""
                    is_quick = "quick apply" in btn_text or "easy apply" in btn_text

                    desc_el = await job_page.query_selector('[data-automation="jobAdDetails"]')
                    desc = (await desc_el.inner_text()).strip() if desc_el else ""

                    marker = "✅ QUICK APPLY" if is_quick else "❌ external"
                    logger.info(f"{marker} | {j['title']} @ {j['company']} | btn='{btn_text}'")

                    if is_quick:
                        j["description"] = desc
                        j["easy_apply"] = True
                        quick_apply_jobs.append(j)
                        if len(quick_apply_jobs) >= 2:
                            await browser.close()
                            return quick_apply_jobs
                except Exception as e:
                    logger.warning(f"Job page error for {j['url']}: {e}")
                finally:
                    await job_page.close()
                await asyncio.sleep(1)

            if len(cards) < 22:
                break
            await asyncio.sleep(2)

        await browser.close()
        return quick_apply_jobs


async def apply_two(jobs: list[dict]):
    """Apply to the given jobs using the applicator."""
    import tracker
    from models import JobListing
    from tailorer import tailor
    from applicator import apply

    tracker.init_db()
    candidate = {
        "name": "Sagar Verma",
        "email": "sagarverma1997@gmail.com",
        "phone": "+61491621148",
    }

    for j in jobs[:2]:
        job = JobListing(
            url=j["url"], title=j["title"], company=j["company"],
            board="seek", description=j.get("description", ""),
            posted_at=j.get("posted_at", ""),
            easy_apply=True,
        )
        logger.info(f"\n{'='*60}")
        logger.info(f"Applying to: {job.title} @ {job.company}")
        logger.info(f"URL: {job.url}")
        try:
            resume_pdf, cover_pdf = await tailor(job)
            logger.info(f"Tailored → resume: {resume_pdf}, cover: {cover_pdf}")
            result = await apply(job, resume_pdf, cover_pdf, candidate)
            logger.info(f"Result: {result} ✅")
        except Exception as e:
            logger.error(f"Failed: {e}")


async def main():
    logger.info("=== STEP 1: Scanning Seek AWS jobs for Quick Apply ===")
    quick_jobs = await scan_quick_apply(max_pages=5)

    logger.info(f"\n=== Found {len(quick_jobs)} Quick Apply jobs ===")
    for j in quick_jobs:
        logger.info(f"  {j['title']} @ {j['company']}")

    if not quick_jobs:
        logger.error("No Quick Apply jobs found — session may be expired. Run: venv/bin/python setup_sessions.py")
        return

    logger.info(f"\n=== STEP 2: Applying to first 2 Quick Apply jobs ===")
    await apply_two(quick_jobs)
    logger.info("\n=== Test complete ===")


if __name__ == "__main__":
    asyncio.run(main())
