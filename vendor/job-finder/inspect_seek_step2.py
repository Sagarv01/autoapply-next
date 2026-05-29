"""
Inspect what Step 2+ of Seek Quick Apply looks like (profile + extra questions).
Also verifies file upload confirmation text.
Run: venv/bin/python inspect_seek_step2.py
"""
import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

SESSION = Path("sessions/seek/state.json")
# A confirmed Quick Apply job
APPLY_URL = "https://au.seek.com/job/91305761/apply"  # Senior Security Architect
RESUME = sorted(Path("output").glob("SagarVerma_TheDecipher*.pdf"))[-1] if list(Path("output").glob("SagarVerma_TheDecipher*.pdf")) else None


async def dump_visible_inputs(page, label):
    data = await page.evaluate("""() => {
        const inputs = document.querySelectorAll(
            'input:not([type=hidden]):not([type=file]), select, textarea, [role=combobox]'
        );
        return Array.from(inputs)
            .filter(el => el.offsetParent !== null)
            .map(el => {
                const lbl = document.querySelector(`label[for="${el.id}"]`);
                const wrappingLabel = el.closest('label');
                return {
                    tag: el.tagName, type: el.getAttribute('type') || '',
                    id: el.id, name: el.name || '',
                    label: lbl?.innerText?.trim() || wrappingLabel?.innerText?.trim() || '',
                    placeholder: el.placeholder || '',
                    value: el.value || '',
                    options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text) : [],
                    required: el.required,
                };
            });
    }""")
    print(f"\n=== Visible inputs on: {label} ===")
    for d in data:
        print(f"  {d}")
    return data


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(storage_state=str(SESSION))
        page = await ctx.new_page()

        await page.goto(APPLY_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        print(f"URL: {page.url}")

        # Check upload confirmation selectors
        print("\n=== Step 1 page ===")
        await dump_visible_inputs(page, "Step 1 - Choose documents")

        # Upload resume
        if RESUME:
            print(f"\nUploading resume: {RESUME}")
            radio = page.locator('input[name="resume-method"][value="upload"]')
            if await radio.count(): await radio.check(); await asyncio.sleep(0.3)
            await page.locator('#resume-fileFile').set_input_files(str(RESUME))
            await asyncio.sleep(1.5)
            # Check for confirmation text
            content = await page.content()
            for phrase in ["resumé attached", "resume attached", "attached", RESUME.name[:20]]:
                if phrase.lower() in content.lower():
                    print(f"  ✅ Upload confirmed: found '{phrase}'")
                    break
            else:
                print("  ⚠️  No upload confirmation text found")

            # Upload cover letter
            cover = sorted(Path("output").glob("CoverLetter_TheDecipher*.pdf"))
            if cover:
                cover = cover[-1]
                print(f"Uploading cover letter: {cover}")
                cr = page.locator('input[name="coverLetter-method"][value="upload"]')
                if await cr.count(): await cr.check(); await asyncio.sleep(0.3)
                await page.locator('#coverLetter-fileFile').set_input_files(str(cover))
                await asyncio.sleep(1.5)
                content = await page.content()
                for phrase in ["cover letter attached", "attached", cover.name[:20]]:
                    if phrase.lower() in content.lower():
                        print(f"  ✅ Cover letter confirmed: found '{phrase}'")
                        break
                else:
                    print("  ⚠️  No cover letter confirmation found")

            # Click Continue
            btn = page.get_by_role("button", name="Continue")
            await btn.first.evaluate("el => el.click()")
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle", timeout=8000)

            print(f"\nURL after Step 1 Continue: {page.url}")
            await dump_visible_inputs(page, "Step 2")

            # Screenshot Step 2
            await page.screenshot(path="/tmp/seek_step2.png", full_page=True)
            print("Screenshot saved: /tmp/seek_step2.png")

            # Click Continue on Step 2
            btn2 = page.get_by_role("button", name="Continue")
            if await btn2.count():
                await btn2.first.evaluate("el => el.click()")
                await asyncio.sleep(3)
                await page.wait_for_load_state("networkidle", timeout=8000)
                print(f"\nURL after Step 2 Continue: {page.url}")
                await dump_visible_inputs(page, "Step 3 / Review")
                await page.screenshot(path="/tmp/seek_step3.png", full_page=True)
                print("Screenshot saved: /tmp/seek_step3.png")

        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
