"""
One-time setup: open the persistent Chrome profile that the LinkedIn bot will
use. Log in manually (including 2FA) once. The profile is auto-saved to
sessions/linkedin_chrome_profile/ and reused on every later run.

Run once:
    venv/bin/python setup_linkedin_profile.py

Steps:
  1. A Chrome window opens at the LinkedIn login page.
  2. Log in with email + password + any 2FA challenge.
  3. Wait until you're back on linkedin.com/feed.
  4. Close the window.

Verify success: re-run this script and you should NOT see the login form
(LinkedIn lands you on /feed directly).
"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

USER_DATA_DIR = Path("sessions/linkedin_chrome_profile").resolve()
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
LOGIN_URL = "https://www.linkedin.com/login"


async def main():
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Chrome profile: {USER_DATA_DIR}")
    print(f"Opening {LOGIN_URL}\n")

    async with async_playwright() as pw:
        kwargs: dict = {
            "user_data_dir": str(USER_DATA_DIR),
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if CHROME_PATH.exists():
            kwargs["executable_path"] = str(CHROME_PATH)
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(LOGIN_URL)
        try:
            await ctx.wait_for_event("close", timeout=0)
        except Exception:
            pass
        print("\n✅ Profile saved.")


if __name__ == "__main__":
    asyncio.run(main())
