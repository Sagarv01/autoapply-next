"""
Inspect Seek Quick Apply form to find exact selectors.
Run: venv/bin/python inspect_seek_form.py
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

SESSION = Path("sessions/seek/state.json")
# Use a known Quick Apply job URL
TEST_URL = "https://au.seek.com/job/91303319/apply"  # Vulnerability Mgmt Specialist


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(storage_state=str(SESSION))
        page = await ctx.new_page()

        print(f"Navigating to: {TEST_URL}")
        await page.goto(TEST_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        print(f"URL: {page.url}")

        # Dump all data-automation attributes on the page
        autos = await page.evaluate("""() => {
            const els = document.querySelectorAll('[data-automation]');
            return Array.from(els).map(el => ({
                automation: el.getAttribute('data-automation'),
                tag: el.tagName,
                text: el.innerText?.trim().slice(0, 60),
                type: el.getAttribute('type'),
                role: el.getAttribute('role'),
            }));
        }""")
        print("\n=== data-automation elements ===")
        for a in autos:
            print(f"  [{a['tag']}] data-automation='{a['automation']}' type={a['type']} role={a['role']} text='{a['text']}'")

        # Find all file inputs
        inputs = await page.evaluate("""() => {
            const els = document.querySelectorAll('input[type=file], input[accept]');
            return Array.from(els).map(el => ({
                id: el.id, name: el.name, accept: el.accept,
                automation: el.getAttribute('data-automation'),
                visible: el.offsetParent !== null,
            }));
        }""")
        print("\n=== File inputs ===")
        for i in inputs:
            print(f"  {i}")

        # Find all buttons
        buttons = await page.evaluate("""() => {
            const els = document.querySelectorAll('button, [role=button], input[type=submit]');
            return Array.from(els).map(el => ({
                tag: el.tagName,
                text: el.innerText?.trim().slice(0, 80),
                automation: el.getAttribute('data-automation'),
                type: el.getAttribute('type'),
            })).filter(b => b.text);
        }""")
        print("\n=== Buttons ===")
        for b in buttons:
            print(f"  [{b['tag']}] text='{b['text']}' automation='{b['automation']}' type={b['type']}")

        # Find radio buttons / checkboxes (for cover letter selection)
        radios = await page.evaluate("""() => {
            const els = document.querySelectorAll('input[type=radio], input[type=checkbox]');
            return Array.from(els).map(el => ({
                id: el.id, name: el.name, value: el.value,
                label: document.querySelector(`label[for="${el.id}"]`)?.innerText?.trim(),
                automation: el.getAttribute('data-automation'),
            }));
        }""")
        print("\n=== Radios/Checkboxes ===")
        for r in radios:
            print(f"  {r}")

        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
