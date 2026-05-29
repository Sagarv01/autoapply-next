"""
One-shot: navigate to a Seek Quick Apply job, reach the resume upload step,
and delete ALL resumes from the library without delay.
"""
import asyncio
import logging
from pathlib import Path
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

JOB_URL       = "https://au.seek.com/job/91369528"
SESSION_STATE = str(Path(__file__).parent / "sessions/seek/state.json")
CHROME_PATH   = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


async def delete_all(page):
    logger.info("Navigating to profile to delete all resumes...")

    while True:
        await page.goto("https://au.seek.com/profile/me", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        # Open resume drawer
        btn = await page.query_selector('[data-automation="resume-edit-link"]')
        if not btn:
            logger.info("No resume button found — done.")
            break
        await btn.click()
        await page.wait_for_timeout(2000)

        # Find all Options buttons
        drawer = await page.query_selector('[data-automation="resume-form-drawer"]')
        options_buttons = await drawer.query_selector_all('button[aria-label^="Options for"]')

        if not options_buttons:
            logger.info("No resumes left — library is clear!")
            break

        logger.info(f"{len(options_buttons)} resume(s) remaining. Deleting...")

        aria = await options_buttons[0].get_attribute("aria-label")
        logger.info(f"Deleting: {aria}")
        await options_buttons[0].click()
        await page.wait_for_timeout(1000)

        delete_option = await page.wait_for_selector(
            'button:has-text("Delete"), [role="menuitem"]:has-text("Delete")',
            timeout=5000,
        )
        await delete_option.click()
        await page.wait_for_timeout(1000)

        try:
            confirm = await page.wait_for_selector('button:has-text("Delete"):visible', timeout=5000)
            await confirm.click()
            logger.info("Confirmed.")
        except Exception:
            pass

        await page.wait_for_timeout(2000)

    logger.info("All resumes deleted.")


async def run():
    launch_kwargs = {"headless": False}
    if CHROME_PATH.exists():
        launch_kwargs["executable_path"] = str(CHROME_PATH)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        ctx     = await browser.new_context(storage_state=SESSION_STATE)
        page    = await ctx.new_page()
        await delete_all(page)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
