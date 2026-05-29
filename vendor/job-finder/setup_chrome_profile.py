"""
One-time setup: open the persistent Chrome profile that the bot will use, so
you can log into Seek manually. After this, the bot reuses the profile
(cookies, history, fingerprint) every run — much harder for Seek to detect
than the prior storage_state approach.

Run once:
    venv/bin/python setup_chrome_profile.py

Log into Seek in the browser window that opens. Then close the window — the
profile is automatically saved to sessions/seek_chrome_profile/.
"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

USER_DATA_DIR = Path("sessions/seek_chrome_profile").resolve()
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
LOGIN_URL = "https://au.seek.com/oauth/login"


async def main():
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Chrome profile: {USER_DATA_DIR}")
    print(f"Opening {LOGIN_URL}\n")
    print("Steps:")
    print("  1. Log in (email + password + any OTP).")
    print("  2. Wait until you're back on the Seek homepage.")
    print("  3. Close the browser window when done — profile auto-saves.\n")

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
        # Wait until the user closes the window
        try:
            await ctx.wait_for_event("close", timeout=0)
        except Exception:
            pass
        print("\n✅ Profile saved. The bot can now run with this session.")


if __name__ == "__main__":
    asyncio.run(main())
