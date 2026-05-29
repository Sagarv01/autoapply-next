"""Open a stuck Seek apply form, navigate to step 'answer employer questions',
and dump every visible form field (label, role, options, required-ness) so we
can see what the bot is missing.

Run: venv/bin/python diag_stuck_form.py <seek_job_url>
"""
import asyncio
import json
import sys
from pathlib import Path

from seek_apply import _PeekSession


async def main(url: str) -> None:
    apply_url = url.rstrip("/") + "/apply"
    session_state = str(Path("sessions/seek/state.json").resolve())
    page = await _PeekSession.get_page(session_state)
    try:
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)

        print(f"current URL: {page.url}")
        if "login" in page.url.lower() or "oauth" in page.url.lower():
            print("REDIRECTED TO LOGIN — session may be stale")
            return

        # Click Continue past the document-upload step (assumes resume already attached)
        for step in range(6):
            await asyncio.sleep(2)
            heading = await page.evaluate(
                "() => document.querySelector('h1, h2, [data-automation*=\"step\"]')?.innerText || ''"
            )
            print(f"\n=== step {step} :: heading='{heading[:80]}' ===")

            # Dump every visible interactive field
            fields = await page.evaluate("""() => {
                const out = [];
                const ROLES = ['radio','checkbox','combobox','textbox','spinbutton','listbox'];
                const all = Array.from(document.querySelectorAll(
                    'input, select, textarea, button, [role="radio"], [role="checkbox"], '
                    + '[role="combobox"], [role="textbox"], [role="listbox"], '
                    + '[role="radiogroup"], [role="group"]'
                ));
                for (const el of all) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const cs = window.getComputedStyle(el);
                    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                    const label = (
                        el.getAttribute('aria-label')
                        || (el.labels && el.labels[0]?.innerText)
                        || el.closest('label')?.innerText
                        || el.placeholder
                        || el.name
                        || ''
                    ).trim().slice(0, 120);
                    out.push({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || el.getAttribute('type') || '',
                        role: el.getAttribute('role') || '',
                        name: el.getAttribute('name') || '',
                        id: el.id || '',
                        label: label,
                        value: (el.value || '').slice(0, 80),
                        checked: el.checked || el.getAttribute('aria-checked') === 'true',
                        required: el.required || el.getAttribute('aria-required') === 'true',
                        placeholder: el.placeholder || '',
                    });
                }
                return out;
            }""")

            print(f"  visible interactive elements: {len(fields)}")
            for f in fields:
                print(f"    {f['tag']:10s} role={f['role']:12s} type={f['type']:10s} "
                      f"req={'Y' if f['required'] else 'n'} ck={'Y' if f['checked'] else 'n'}  "
                      f"label={f['label']!r}")

            # Look for visible validation messages
            errors = await page.evaluate("""() => Array.from(
                document.querySelectorAll('[aria-invalid="true"], [role="alert"], [data-automation*="error"]')
            ).map(e => (e.innerText || '').trim()).filter(Boolean)""")
            if errors:
                print(f"  visible errors: {errors}")

            # Try Continue button
            cont_btn = page.locator('button:has-text("Continue"), button:has-text("Submit")').first
            if await cont_btn.count() == 0:
                print("  no Continue/Submit button — stopping")
                break
            try:
                await cont_btn.scroll_into_view_if_needed(timeout=3000)
                await cont_btn.evaluate("el => el.click()")
                await asyncio.sleep(2)
            except Exception as e:
                print(f"  Continue click failed: {type(e).__name__}: {str(e)[:120]}")
                break

    finally:
        try:
            await page.close()
        except Exception:
            pass
        await _PeekSession.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: diag_stuck_form.py <seek_job_url>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
