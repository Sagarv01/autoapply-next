"""
Open Seek's Applied Jobs page and dump every application visible, with timestamps.
Tells us definitively whether Showpad DevOps Engineer is actually in the list
or whether the heuristic verifier returned a false positive.
"""
import asyncio
import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("check")

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

        await page.goto("https://au.seek.com/profile/applied-jobs",
                        wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(4)

        url = page.url.lower()
        if "login" in url or "oauth" in url:
            log.error("Session expired — please re-run setup_sessions.py")
            await browser.close()
            return

        await page.screenshot(path="errors/applied_jobs_check.png", full_page=True)

        rows = await page.evaluate("""() => {
            // Try common Seek selectors for application cards
            const cards = document.querySelectorAll(
                '[data-automation*="application"], [data-automation*="job-card"], article, [role="listitem"]'
            );
            const out = [];
            cards.forEach(c => {
                const text = c.innerText?.trim() || '';
                if (!text || text.length < 20) return;
                const link = c.querySelector('a[href*="/job/"]')?.href || '';
                out.push({text: text.slice(0, 400), link});
            });
            return out;
        }""")

        log.info(f"Found {len(rows)} application cards on page")
        showpad_hits = [r for r in rows if "showpad" in r['text'].lower() or "devops engineer" in r['text'].lower()]
        log.info(f"Cards mentioning Showpad/DevOps Engineer: {len(showpad_hits)}")
        for r in rows[:10]:
            log.info("---")
            log.info(r['text'][:300])
            if r['link']: log.info(f"  link: {r['link']}")

        # Also dump full body text just in case the structured query missed it
        body_text = (await page.locator("body").inner_text()).lower()
        log.info(f"\n=== body text searches ===")
        log.info(f"  'showpad' present: {'showpad' in body_text}")
        log.info(f"  'devops engineer' present: {'devops engineer' in body_text}")
        log.info(f"  '91581982' (Showpad job id) present: {'91581982' in body_text}")

        await asyncio.sleep(30)  # leave open so user can also inspect
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
