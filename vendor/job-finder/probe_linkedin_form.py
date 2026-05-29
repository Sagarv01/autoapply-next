"""Karpathy probe v2: use the accessibility tree (cross-frame, cross-shadow-DOM)
to find form fields on Cuscal Easy Apply. If a11y tree shows them → use
get_by_role to act on them, regardless of iframe scope."""
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from playwright.async_api import async_playwright

URL = "https://www.linkedin.com/jobs/view/4398304596/"
USER_DATA_DIR = Path("sessions/linkedin_chrome_profile").resolve()
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
RESUME = sorted(Path("output").glob("SagarVerma_*.pdf"), key=lambda p: p.stat().st_mtime)[-1]

# Roles that represent fillable form fields
FORM_ROLES = {"combobox", "textbox", "checkbox", "radio", "spinbutton",
              "listbox", "switch", "slider"}


def _prop(node, key):
    """CDP nodes store role/name/value as {'value': ...} structs."""
    v = node.get(key)
    if isinstance(v, dict):
        return str(v.get("value", ""))
    return str(v or "")


async def probe(page, label):
    print(f"\n=== {label} ===")
    client = await page.context.new_cdp_session(page)
    await client.send("Accessibility.enable")
    result = await client.send("Accessibility.getFullAXTree")
    nodes = result.get("nodes", [])
    print(f"  Total a11y nodes (all frames merged): {len(nodes)}")
    fields = []
    for n in nodes:
        role = _prop(n, "role")
        if role not in FORM_ROLES:
            continue
        name = _prop(n, "name")
        value = _prop(n, "value")
        if name or value:
            fields.append((role, name, value))
    print(f"  Form-role nodes with a name/value: {len(fields)}")
    for role, name, value in fields[:25]:
        print(f"    [{role:12s}] {name[:80]!r}  value={value[:40]!r}")
    await client.detach()


async def main():
    async with async_playwright() as pw:
        kwargs = {
            "user_data_dir": str(USER_DATA_DIR),
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if CHROME.exists():
            kwargs["executable_path"] = str(CHROME)
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print(f"Opening {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)

        easy = page.locator('button[aria-label*="Easy Apply"], a[aria-label*="Easy Apply"]').first
        await easy.wait_for(timeout=10000)
        await easy.click()
        await asyncio.sleep(3)
        await probe(page, "After Easy Apply click")

        for i in range(1, 6):
            sub = page.locator('button[aria-label*="Submit application"]')
            if await sub.count() > 0:
                print(f"\n=== Submit reachable at iter {i} ===")
                break
            fi = page.locator('input[type=file]')
            if await fi.count() > 0:
                print(f"  📎 uploading to {await fi.count()} file inputs")
                for j in range(await fi.count()):
                    await fi.nth(j).set_input_files(str(RESUME))
                await asyncio.sleep(4)
            nxt = page.locator(
                'button[aria-label*="Continue to next step"], '
                'button[aria-label*="Review"], button:has-text("Next")'
            )
            if await nxt.count() == 0:
                print(f"  ❌ No Next at iter {i}")
                break
            await nxt.first.click()
            await asyncio.sleep(2.5)
            await probe(page, f"After Next #{i}")

        print("\nBrowser stays open 20s.")
        await asyncio.sleep(20)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
