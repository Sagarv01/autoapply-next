"""Cheap one-shot: open ONE LinkedIn job, scrape title+company+desc with the
chrome-cleanup logic, save to errors/linkedin_test/jd_<id>.txt, exit.
No scoring, no tailoring, no apply."""
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright
from test_linkedin_upload import scrape_job_meta

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.linkedin.com/jobs/view/4392849282/"
JOB_ID = URL.rstrip("/").split("/")[-1]
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
        await asyncio.sleep(2)
        # Click "...more" to expand
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
        out = Path("errors/linkedin_test")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"jd_{JOB_ID}.txt"
        path.write_text(
            f"TITLE: {meta['title']}\nCOMPANY: {meta.get('company', '')}\nURL: {URL}\n\n{meta.get('desc', '')}"
        )
        print(f"\n📄 desc {len(meta.get('desc', ''))} chars")
        print(f"saved → {path.resolve()}")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
