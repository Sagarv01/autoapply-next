"""
Check Seek login state and show what apply buttons look like on a job page.
Run: venv/bin/python check_seek_login.py
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

SESSION = Path("sessions/seek/state.json")


async def main():
    async with async_playwright() as pw:
        ctx_opts = {}
        if SESSION.exists():
            ctx_opts["storage_state"] = str(SESSION)
            print(f"Loading session from {SESSION}")
        else:
            print("No session file found!")

        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(**ctx_opts)
        page = await ctx.new_page()

        # 1. Check login state
        print("\n--- Checking login state ---")
        await page.goto("https://au.seek.com/jobs?keywords=AWS&where=Australia&sortmode=ListedDate", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        sign_in = await page.query_selector('[data-automation="sign in"], a[href*="login"]')
        account = await page.query_selector('[data-automation="account menu"]')
        print(f"Sign-in link present: {sign_in is not None}")
        print(f"Account menu present: {account is not None}")
        print(f"Current URL: {page.url}")

        # 2. Check first job for apply button type
        print("\n--- Checking apply buttons on first few jobs ---")
        cards = await page.query_selector_all('[data-automation="normalJob"]')
        print(f"Found {len(cards)} job cards")

        for i, card in enumerate(cards[:5]):
            link = await card.query_selector('a[data-automation="jobTitle"]')
            if not link:
                continue
            href = await link.get_attribute("href")
            url = f"https://au.seek.com{href}".split("?")[0]
            title = (await link.inner_text()).strip()
            card_text = (await card.inner_text()).lower()

            # Check for any apply-related text in card
            apply_hints = [w for w in ["quick apply", "easy apply", "apply", "external"] if w in card_text]
            print(f"  Card {i+1}: {title}")
            print(f"    Apply hints in card text: {apply_hints}")

            # Visit job page to check apply button
            job_page = await ctx.new_page()
            try:
                await job_page.goto(url, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                apply_btn = await job_page.query_selector('[data-automation="job-detail-apply"]')
                if apply_btn:
                    btn_text = (await apply_btn.inner_text()).strip()
                    print(f"    Apply button text: '{btn_text}'")
                else:
                    # Try other selectors
                    btns = await job_page.query_selector_all('button, a')
                    apply_texts = []
                    for btn in btns:
                        t = (await btn.inner_text()).strip().lower()
                        if "apply" in t and len(t) < 30:
                            apply_texts.append(t)
                    print(f"    Apply buttons found: {apply_texts[:5]}")
            finally:
                await job_page.close()

        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
