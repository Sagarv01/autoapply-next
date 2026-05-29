"""One-shot diagnostic — opens a candidate Seek apply URL with the bot's
exact persistent chrome profile and reports:
  - final URL after redirects
  - logged-in vs login-redirect
  - presence of the Quick Apply markers the bot looks for
  - alternative selectors that DO match (clue: Seek may have renamed them)
  - first 4000 chars of HTML for visual inspection
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
USER_DATA_DIR = Path("sessions/seek_chrome_profile").resolve()


async def main(job_url: str):
    apply_url = job_url.rstrip("/") + "/apply"
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        kwargs = {
            "user_data_dir": str(USER_DATA_DIR),
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if CHROME_PATH.exists():
            kwargs["executable_path"] = str(CHROME_PATH)
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
        page = await ctx.new_page()
        try:
            print(f"Navigating to: {apply_url}")
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            print(f"\nFinal URL:  {page.url}")
            print(f"Page title: {await page.title()}")

            url = page.url.lower()
            if "login" in url or "oauth" in url or "signin" in url:
                print("\n>> RESULT: login redirect — session is invalid on this domain")
            elif "seek.com" not in url:
                print("\n>> RESULT: redirected off Seek")
            else:
                print("\n>> RESULT: still on Seek apply page")

            # Test the EXACT marker the bot looks for
            old_marker_count = await page.locator(
                '#resume-fileFile, input[name="resume-method"]'
            ).count()
            print(f"\nBot's Quick Apply marker count: {old_marker_count}")

            # Probe alternative resume-related selectors that Seek may now use.
            probes = [
                ('input[type="file"]',                  "any file input"),
                ('input[name*="resume"]',               "input name~=resume"),
                ('input[id*="resume"]',                 "input id~=resume"),
                ('input[id*="Resume"]',                 "input id~=Resume"),
                ('input[type="radio"][value*="upload"]', "radio value~=upload"),
                ('input[type="radio"][value*="profile"]', "radio value~=profile"),
                ('button:has-text("Continue")',          "Continue button"),
                ('button:has-text("Submit")',            "Submit button"),
                ('[data-automation*="resume"]',          "data-automation~=resume"),
                ('[data-automation*="apply"]',           "data-automation~=apply"),
                ('[data-testid*="resume"]',              "data-testid~=resume"),
                ('legend',                               "legends"),
                ('h1',                                   "h1 headings"),
            ]
            print("\n=== alternative selector probes ===")
            for sel, desc in probes:
                n = await page.locator(sel).count()
                print(f"  {n:>3}  {desc:35s}  selector={sel}")

            # Capture the H1 + first legend text — clue what page we're really on.
            for sel in ('h1', 'legend'):
                els = page.locator(sel)
                n = await els.count()
                for i in range(min(n, 3)):
                    txt = (await els.nth(i).inner_text()).strip()[:120]
                    print(f"  {sel}[{i}] = {txt!r}")

            # Save raw HTML for visual inspection
            html = await page.content()
            Path("errors").mkdir(exist_ok=True)
            outfile = Path("errors/diagnose_peek_last.html")
            outfile.write_text(html)
            print(f"\nFull HTML saved to: {outfile} ({len(html)} bytes)")
            print(f"\nFirst 1500 chars of <body>:")
            body_start = html.lower().find("<body")
            print(html[body_start:body_start + 1500])

        finally:
            await asyncio.sleep(2)
            await ctx.close()


if __name__ == "__main__":
    job_url = (sys.argv[1] if len(sys.argv) > 1
               else "https://au.seek.com/job/91893419")  # arbitrary recent
    asyncio.run(main(job_url))
