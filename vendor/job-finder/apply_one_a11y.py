"""One-job end-to-end test using the a11y-tree approach. Target: Cuscal
(which broke the old JS scraper). Success = actually submit the job."""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from playwright.async_api import async_playwright
import yaml
from models import JobListing
from tailorer import tailor
from test_linkedin_upload import scrape_job_meta
from linkedin_a11y_apply import walk_and_apply

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.linkedin.com/jobs/view/4398304596/"
JOB_ID = URL.rstrip("/").split("/")[-1]
USER_DATA_DIR = Path("sessions/linkedin_chrome_profile").resolve()
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


async def main():
    cfg = yaml.safe_load(open("config.yaml"))
    candidate = cfg["candidate"]

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
                '.job-details-jobs-unified-top-card__job-title, h1', timeout=20000
            )
        except Exception:
            print("   ⚠️  h1 not found in 20s — proceeding anyway")
        await asyncio.sleep(3)
        # Expand "...more"
        more = page.locator(
            'button.show-more-less-html__button, button[aria-label*="see more"]'
        )
        if await more.count():
            try: await more.first.click(timeout=3000)
            except Exception: pass
            await asyncio.sleep(1.5)
        meta = await scrape_job_meta(page)
        print(f"Title: {meta['title']!r}")
        print(f"Desc: {len(meta.get('desc') or '')} chars")
        job = JobListing(
            url=URL, title=meta["title"], company=meta.get("company") or "Cuscal",
            board="linkedin", description=meta.get("desc") or "", easy_apply=True,
        )

        print("\nTailoring ...")
        resume_pdf, cover_pdf = await tailor(job, tier="full")
        print(f"  {Path(resume_pdf).name}")
        print(f"  {Path(cover_pdf).name}")

        print("\n→ A11y-based apply walk ...")
        outcome = await walk_and_apply(page, job, resume_pdf, cover_pdf, candidate)
        print(f"\nOutcome: {outcome}")

        if outcome == "applied":
            print("\nVerifying on Applied Jobs ...")
            await page.goto("https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED",
                           wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(4)
            body = (await page.content()).lower()
            if JOB_ID in body:
                print(f"✅ VERIFIED: {JOB_ID} on Applied Jobs page")
            else:
                print(f"❌ NOT VERIFIED: {JOB_ID} missing")

        print("\nBrowser stays 30s.")
        await asyncio.sleep(30)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
