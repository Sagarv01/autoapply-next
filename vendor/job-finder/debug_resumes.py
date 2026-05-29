"""
Debug — open resume drawer and inspect the HTML inside each resume item.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

SESSION_STATE = str(Path(__file__).parent / "sessions/seek/state.json")
CHROME_PATH   = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

async def run():
    launch_kwargs = {"headless": False}
    if CHROME_PATH.exists():
        launch_kwargs["executable_path"] = str(CHROME_PATH)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        ctx     = await browser.new_context(storage_state=SESSION_STATE)
        page    = await ctx.new_page()

        await page.goto("https://au.seek.com/profile/me", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        # Open the drawer
        btn = await page.query_selector('[data-automation="resume-edit-link"]')
        await btn.click()
        await page.wait_for_timeout(2000)

        # Screenshot the drawer zoomed in
        drawer = await page.query_selector('[data-automation="resume-form-drawer"]')
        if drawer:
            await drawer.screenshot(path="debug_drawer.png")
            print("Drawer screenshot: debug_drawer.png")

            # Dump full inner HTML of the drawer
            html = await drawer.inner_html()
            print("\n--- Drawer inner HTML ---")
            print(html[:5000])

            # Find all buttons inside the drawer
            print("\n--- Buttons inside drawer ---")
            buttons = await drawer.query_selector_all("button")
            for b in buttons:
                text  = (await b.inner_text()).strip()
                auto  = await b.get_attribute("data-automation") or ""
                aria  = await b.get_attribute("aria-label") or ""
                title = await b.get_attribute("title") or ""
                print(f"  text={text!r:30} data-automation={auto!r:40} aria={aria!r} title={title!r}")

            # Find all elements inside each resume-item
            print("\n--- Resume items ---")
            items = await drawer.query_selector_all('[data-automation^="resume-item-"]')
            for item in items:
                auto = await item.get_attribute("data-automation")
                text = (await item.inner_text())[:100].strip()
                print(f"\n  Item: {auto}")
                print(f"  Text: {text!r}")
                item_html = await item.inner_html()
                print(f"  HTML: {item_html[:500]}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
