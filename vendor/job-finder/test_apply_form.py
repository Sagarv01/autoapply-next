"""
Diagnostic script: walks through a Seek Quick Apply form step-by-step.
Dumps every visible input, radio group, and select at each step.
Does NOT submit — stops at the Review page.

Usage: venv/bin/python test_apply_form.py <seek_job_url>
e.g.:  venv/bin/python test_apply_form.py https://au.seek.com/job/91318570
"""
import asyncio
import json
import sys
from pathlib import Path
from playwright.async_api import async_playwright

SESSION = Path("sessions/seek/state.json")
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
RESUME = sorted(Path("output").glob("SagarVerma_*.pdf"))[-1] if list(Path("output").glob("SagarVerma_*.pdf")) else None


def clean_session(src: Path) -> str:
    bad = ("partitionKey", "priority", "sourceScheme", "sourcePort",
           "session", "size", "expirationDate", "storeId")
    data = json.loads(src.read_text())
    for c in data.get("cookies", []):
        for f in bad:
            c.pop(f, None)
    out = Path("/tmp/seek_test_session.json")
    out.write_text(json.dumps(data))
    return str(out)


async def dump_step(page, label: str):
    print(f"\n{'='*60}")
    print(f"STEP: {label}  |  URL: {page.url}")
    print('='*60)

    # Text/select/textarea inputs
    inputs = await page.evaluate("""() => {
        const els = document.querySelectorAll(
            'input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]), select, textarea'
        );
        return Array.from(els).filter(e => e.offsetParent !== null).map(e => {
            const lbl = document.querySelector(`label[for="${e.id}"]`);
            return {
                tag: e.tagName, type: e.getAttribute('type') || '',
                id: e.id, name: e.name,
                label: lbl?.innerText?.trim() || '',
                placeholder: e.placeholder || '',
                value: e.value || '',
                options: e.tagName === 'SELECT'
                    ? Array.from(e.options).map(o => o.text) : [],
                required: e.required,
            };
        });
    }""")

    if inputs:
        print(f"\n  TEXT/SELECT INPUTS ({len(inputs)}):")
        for i in inputs:
            print(f"    [{i['tag']}] id='{i['id']}' name='{i['name']}' "
                  f"label='{i['label']}' placeholder='{i['placeholder']}' "
                  f"value='{i['value']}' required={i['required']}")
            if i['options']:
                print(f"      options: {i['options'][:5]}{'...' if len(i['options'])>5 else ''}")
    else:
        print("\n  No text/select inputs found.")

    # Radio groups
    radio_groups = await page.evaluate("""() => {
        const radios = document.querySelectorAll('input[type=radio]');
        const groups = {};
        radios.forEach(r => {
            if (!r.offsetParent) return;
            if (!groups[r.name]) groups[r.name] = [];
            const lbl = document.querySelector(`label[for="${r.id}"]`);
            groups[r.name].push({
                id: r.id, value: r.value, checked: r.checked,
                label: lbl?.innerText?.trim() || r.value,
            });
        });
        return Object.entries(groups).map(([name, opts]) => ({name, opts}));
    }""")

    if radio_groups:
        print(f"\n  RADIO GROUPS ({len(radio_groups)}):")
        for g in radio_groups:
            print(f"    Group '{g['name']}':")
            for o in g['opts']:
                print(f"      {'[x]' if o['checked'] else '[ ]'} id='{o['id']}' "
                      f"value='{o['value']}' label='{o['label']}'")
    else:
        print("\n  No radio groups found.")

    # Buttons
    btns = await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('button'))
            .filter(b => b.offsetParent !== null)
            .map(b => b.innerText.trim());
    }""")
    print(f"\n  VISIBLE BUTTONS: {btns}")


async def main():
    job_url = sys.argv[1] if len(sys.argv) > 1 else None
    if not job_url:
        print("Usage: venv/bin/python test_apply_form.py <seek_job_url>")
        sys.exit(1)

    apply_url = job_url.rstrip("/") + "/apply"
    session = clean_session(SESSION)

    if not RESUME:
        print("No resume found in output/ — run the bot once to generate one, or add a PDF manually.")
        sys.exit(1)

    print(f"Testing: {apply_url}")
    print(f"Resume:  {RESUME}")

    async with async_playwright() as pw:
        kwargs = {"headless": False}
        if CHROME.exists():
            kwargs["executable_path"] = str(CHROME)
        browser = await pw.chromium.launch(**kwargs)
        ctx = await browser.new_context(storage_state=session)
        page = await ctx.new_page()

        await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        print(f"\nFinal URL after load: {page.url}")
        if "login" in page.url or "oauth" in page.url:
            print("❌ Redirected to login — session expired. Run setup_sessions.py")
            await browser.close()
            return

        # Check for Quick Apply form
        marker = await page.locator('#resume-fileFile, input[name="resume-method"]').count()
        if not marker:
            print("❌ Not a Quick Apply form — no resume upload input found")
            await browser.close()
            return

        print("✅ Quick Apply form detected")

        # Step 1: dump then upload
        await dump_step(page, "Step 1 — Choose documents")

        print(f"\n  Selecting 'Upload a resumé' radio...")
        radio = page.locator('input[name="resume-method"][value="upload"]')
        if await radio.count():
            await radio.check()
            await asyncio.sleep(1.5)
            print("  ✅ Radio selected")
        else:
            print("  ⚠️  No upload radio found")

        print(f"  Uploading resume: {RESUME.name}")
        file_input = page.locator('#resume-fileFile')
        await file_input.set_input_files(str(RESUME))
        await asyncio.sleep(2)

        # Check confirmation
        content = (await page.content()).lower()
        stem = RESUME.stem[:25].lower()
        if stem in content:
            print(f"  ✅ Resume confirmed: {RESUME.name}")
        else:
            print(f"  ❌ Resume NOT confirmed — '{stem}' not found in page")
            print("  Checking for generic 'attached':", "attached" in content)

        # Click Continue
        print("\n  Clicking Continue...")
        btn = page.get_by_role("button", name="Continue")
        await btn.first.scroll_into_view_if_needed(timeout=5000)
        await btn.first.evaluate("el => el.click()")
        await asyncio.sleep(3)

        # Walk remaining steps
        for step_num in range(2, 7):
            await asyncio.sleep(1)
            url = page.url.lower()
            if "review" in url or "/review" in url:
                print(f"\n✅ Reached Review page — stopping (not submitting)")
                break

            # Detect step name
            active = page.locator("button[aria-current='step'], button[aria-selected='true']")
            step_name = (await active.first.inner_text()).strip() if await active.count() else f"Step {step_num}"

            await dump_step(page, step_name)

            # Click Continue
            print(f"\n  Clicking Continue on '{step_name}'...")
            btn = page.get_by_role("button", name="Continue")
            if await btn.count():
                await btn.first.scroll_into_view_if_needed(timeout=5000)
                await btn.first.evaluate("el => el.click()")
                await asyncio.sleep(3)
            else:
                print("  ⚠️  No Continue button found — stopping")
                break

        print("\n\nDone. Browser staying open for 15s so you can inspect...")
        await asyncio.sleep(15)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
