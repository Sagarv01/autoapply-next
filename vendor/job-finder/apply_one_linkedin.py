"""Run the LinkedIn end-to-end pipeline on ONE hardcoded URL — for fast verification."""
import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
assert os.environ.get("ANTHROPIC_API_KEY")
assert os.environ.get("OPENAI_API_KEY")

from playwright.async_api import async_playwright
from matcher import score_job
from tailorer import tailor
from models import JobListing
from test_linkedin_upload import scrape_job_meta
from linkedin_search_and_apply import apply_easy_apply

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.linkedin.com/jobs/view/4392849282/"
USER_DATA_DIR = Path("sessions/linkedin_chrome_profile").resolve()
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


async def main():
    async with async_playwright() as pw:
        kwargs = {
            "user_data_dir": str(USER_DATA_DIR),
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if CHROME.exists():
            kwargs["executable_path"] = str(CHROME)
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print(f"Opening {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector(
                '.job-details-jobs-unified-top-card__job-title, h1', timeout=15000
            )
        except Exception:
            pass
        await asyncio.sleep(3)

        more_btn = page.locator(
            'button.show-more-less-html__button, '
            'button[aria-label*="see more"], button[aria-label*="Show more"], '
            'button.jobs-description__footer-button'
        )
        if await more_btn.count() > 0:
            try:
                await more_btn.first.click(timeout=3000)
                await asyncio.sleep(1.5)
            except Exception:
                pass

        meta = await scrape_job_meta(page)
        print(f"Title: {meta['title']!r}")
        print(f"Desc length (after chrome-strip): {len(meta.get('desc') or '')} chars")

        job = JobListing(
            url=URL, title=meta["title"], company=meta.get("company") or "ITbility",
            board="linkedin", description=meta.get("desc") or "", easy_apply=True,
        )
        score, reasoning = await score_job(job)
        print(f"📊 {score}% — {reasoning[:120]}")

        print("\n→ Tailoring (Sonnet) ...")
        resume_pdf, cover_pdf = await tailor(job, tier="full")
        print(f"   {Path(resume_pdf).name}")
        print(f"   {Path(cover_pdf).name}")

        print("\n→ Apply Easy Apply ...")
        outcome = await apply_easy_apply(page, job, resume_pdf, cover_pdf)
        print(f"   outcome = {outcome}")

        print("\n(browser stays open 30s)")
        await asyncio.sleep(30)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
