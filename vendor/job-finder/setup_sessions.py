"""
One-time manual login setup for Seek and Indeed.

Run this once to save your browser sessions:
    source venv/bin/activate
    python setup_sessions.py

A browser window will open for each board. Log in manually (including any OTP),
then press Enter in the terminal to save the session and move to the next board.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import random

SESSION_DIR = Path("sessions")

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

BOARDS = [
    {
        "name": "Seek",
        "dir": SESSION_DIR / "seek",
        "url": "https://au.seek.com/oauth/login",
    },
]


async def setup_board(pw, board: dict):
    board["dir"].mkdir(parents=True, exist_ok=True)
    state_file = board["dir"] / "state.json"

    print(f"\n{'='*50}")
    print(f"  Setting up: {board['name']}")
    print(f"{'='*50}")
    print(f"  A browser window will open at: {board['url']}")
    print(f"  Log in manually (including any OTP).")
    print(f"  The session will be saved automatically once login is detected.")
    print()

    # Use real Chrome if available (bypasses Indeed's bot detection), else fall back to Chromium
    import shutil
    chrome_path = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if shutil.which("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome") or
           __import__('pathlib').Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists()
        else None
    )
    launch_args = {"headless": False, "args": ["--no-sandbox"]}
    if chrome_path:
        launch_args["executable_path"] = chrome_path
        print(f"  Using real Chrome browser (better compatibility).")
    else:
        print(f"  Using Playwright Chromium.")

    browser = await pw.chromium.launch(**launch_args)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
    )

    page = await context.new_page()
    await page.goto(board["url"], wait_until="domcontentloaded")

    # Wait for user to log in — poll every 5 seconds for up to 3 minutes
    print(f"  Waiting for you to log in (up to 5 minutes)...")
    for i in range(60):  # 60 x 5s = 5 minutes
        await asyncio.sleep(5)
        current_url = page.url
        if board["name"] == "LinkedIn" and ("feed" in current_url or "mynetwork" in current_url or ("linkedin.com" in current_url and "login" not in current_url and "checkpoint" not in current_url)):
            print(f"  Detected successful LinkedIn login!")
            await asyncio.sleep(3)
            break
        if board["name"] == "Seek" and ("login" not in current_url and "oauth" not in current_url):
            print(f"  Detected successful Seek login!")
            await asyncio.sleep(3)
            break
        if board["name"] == "Indeed":
            # Indeed OTP page stays on secure.indeed.com — detect login by landing on au.indeed.com jobs page
            if "au.indeed.com" in current_url and "account" not in current_url and "auth" not in current_url:
                print(f"  Detected successful Indeed login!")
                await asyncio.sleep(3)
                break
            # Also accept indeedaccount.com redirect as logged in
            if "indeedaccount.com" not in current_url and "secure.indeed.com" not in current_url and "indeed.com" in current_url:
                print(f"  Detected successful Indeed login!")
                await asyncio.sleep(3)
                break
        remaining = (60 - i - 1) * 5
        if remaining > 0 and i % 6 == 5:  # print every 30s
            print(f"  Still waiting... {remaining}s remaining. Current URL: {current_url}")
    else:
        print(f"  Timed out waiting for login. Saving whatever session state exists.")

    # Save session
    await context.storage_state(path=str(state_file))

    # Strip Chrome-only cookie fields that Playwright rejects on reload
    import json
    raw = json.loads(state_file.read_text())
    for cookie in raw.get("cookies", []):
        for field in ("partitionKey", "priority", "sourceScheme", "sourcePort", "session", "size", "expirationDate", "storeId"):
            cookie.pop(field, None)
    state_file.write_text(json.dumps(raw))
    print(f"  Session saved to {state_file}")

    await context.close()
    await browser.close()


async def main():
    print("\nJob Bot — Manual Session Setup")
    print("This will open browser windows for Seek and Indeed.")
    print("Log in to each one, then press Enter to save the session.\n")

    async with async_playwright() as pw:
        for board in BOARDS:
            await setup_board(pw, board)

    print("\nAll sessions saved. You can now run the bot:")
    print("  python main.py\n")


if __name__ == "__main__":
    asyncio.run(main())
