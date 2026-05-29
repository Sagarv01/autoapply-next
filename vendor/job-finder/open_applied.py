"""Open Seek's Applied Jobs page so the user can verify it's the right URL."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://au.seek.com/profile/applied-jobs"
SESSION = str(Path("sessions/seek/state.json").resolve())
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


async def main():
    async with async_playwright() as pw:
        kwargs = {"headless": False}
        if CHROME.exists():
            kwargs["executable_path"] = str(CHROME)
        browser = await pw.chromium.launch(**kwargs)
        ctx = await browser.new_context(storage_state=SESSION)
        page = await ctx.new_page()
        print(f"Opening: {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        print(f"Landed on: {page.url}")
        print("Browser stays open for 5 minutes — inspect freely, then close manually.")
        await asyncio.sleep(300)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
