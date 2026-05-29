"""
Seek Resume Library Cleaner
- Opens the resume drawer on the Seek profile page
- Clicks the Options menu on a resume → Delete → Confirm Delete
- 30s delay between each deletion
- If no resumes to delete, waits 10 minutes then checks again
- Runs infinitely alongside the main bot
"""
import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s resume_cleaner — %(message)s",
)
logger = logging.getLogger(__name__)

PROFILE_URL   = "https://au.seek.com/profile/me"
SESSION_STATE = str(Path(__file__).parent / "sessions/seek/state.json")
CHROME_PATH   = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DELETE_DELAY  = 30   # seconds between deletions
EMPTY_DELAY   = 300  # seconds to wait when nothing to delete


async def open_resume_drawer(page):
    """Navigate to profile and open the resume management drawer."""
    await page.goto(PROFILE_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)
    btn = await page.query_selector('[data-automation="resume-edit-link"]')
    if not btn:
        raise Exception("Could not find resume-edit-link button on profile page")
    await btn.click()
    await page.wait_for_timeout(2000)


async def delete_one_resume(page) -> bool:
    """
    Opens the Options menu on the first resume and deletes it.
    Returns True if deleted, False if no resumes found.
    """
    await open_resume_drawer(page)

    # Find all "Options for ..." buttons inside the drawer
    drawer = await page.query_selector('[data-automation="resume-form-drawer"]')
    if not drawer:
        logger.warning("Resume drawer not found.")
        return False

    options_buttons = await drawer.query_selector_all('button[aria-label^="Options for"]')

    if not options_buttons:
        logger.info("No resumes found in library.")
        return False

    total = len(options_buttons)
    logger.info(f"Found {total} resume(s). Deleting one...")

    # Click the first "Options" button
    aria = await options_buttons[0].get_attribute("aria-label")
    logger.info(f"Opening options for: {aria}")
    await options_buttons[0].click()
    await page.wait_for_timeout(1000)

    # Click "Delete" in the dropdown menu
    delete_option = await page.wait_for_selector(
        'button:has-text("Delete"), [role="menuitem"]:has-text("Delete")',
        timeout=5000,
    )
    await delete_option.click()
    await page.wait_for_timeout(1000)

    # Confirm deletion in the modal
    try:
        confirm_btn = await page.wait_for_selector(
            'button:has-text("Delete"):visible',
            timeout=5000,
        )
        await confirm_btn.click()
        logger.info("Confirmed deletion.")
    except Exception:
        logger.info("No confirmation modal — deletion completed directly.")

    await page.wait_for_timeout(2000)
    logger.info("Resume deleted successfully.")
    return True


async def make_page(pw):
    """Launch a fresh browser + page."""
    launch_kwargs = {"headless": False}
    if CHROME_PATH.exists():
        launch_kwargs["executable_path"] = str(CHROME_PATH)
    browser = await pw.chromium.launch(**launch_kwargs)
    ctx     = await browser.new_context(storage_state=SESSION_STATE)
    page    = await ctx.new_page()
    return browser, page


async def run():
    logger.info("Resume cleaner started. Running indefinitely.")

    async with async_playwright() as pw:
        browser, page = await make_page(pw)

        while True:
            try:
                deleted = await delete_one_resume(page)

                if deleted:
                    logger.info(f"Waiting {DELETE_DELAY}s before next deletion...")
                    await asyncio.sleep(DELETE_DELAY)
                else:
                    logger.info(f"Library empty. Checking again in {EMPTY_DELAY // 60} minutes...")
                    await asyncio.sleep(EMPTY_DELAY)

            except Exception as e:
                logger.error(f"Error: {e}. Restarting browser in 10s...")
                try:
                    await browser.close()
                except Exception:
                    pass
                await asyncio.sleep(10)
                browser, page = await make_page(pw)


if __name__ == "__main__":
    asyncio.run(run())
