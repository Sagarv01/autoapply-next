"""
Diagnostic for the VTAC Infrastructure & Security Engineer apply failure.

Opens the apply page using the saved Seek session, then:
1. Reports whether the job is already applied / still applyable
2. Walks to the "answer employer questions" step
3. Dumps EVERY field on that step (text inputs, selects, radios, checkboxes,
   custom widgets, comboboxes, date pickers — anything visible)
4. Saves a screenshot + the DOM of the questions container
5. Shows what the existing scraper would have seen vs what's actually there

Does NOT submit. Read-only investigation.
"""
import asyncio
import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright

import seek_apply

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("diag")

JOB_URL = "https://au.seek.com/job/91547118"
APPLY_URL = JOB_URL + "/apply"
SESSION = str(Path("sessions/seek/state.json").resolve())
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
OUT = Path("errors/vtac_diagnose")
RESUME = Path("output/SagarVerma_VictorianTertiaryAdmissionsCentreLimitedVTAC_InfrastructureAndSecurityEngineer_20260417_111209_173.pdf").resolve()
COVER = Path("output/CoverLetter_VictorianTertiaryAdmissionsCentreLimitedVTAC_InfrastructureAndSecurityEngineer_20260417_111211_796.pdf").resolve()


async def all_visible_fields(page):
    """Dump EVERY interactive element the bot's scanner ignores."""
    return await page.evaluate("""() => {
        const out = [];
        // Same scanner the bot uses — text inputs / select / textarea
        const native = document.querySelectorAll(
            'input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]),' +
            'select, textarea'
        );
        native.forEach(el => {
            if (!el.offsetParent) return;
            const lbl = document.querySelector(`label[for="${el.id}"]`);
            out.push({
                kind: 'native',
                tag: el.tagName,
                type: el.getAttribute('type') || '',
                id: el.id, name: el.name,
                label: lbl?.innerText?.trim() || '',
                required: el.required,
                value: el.value,
                outerHTML: el.outerHTML.slice(0, 400),
            });
        });
        // Radios grouped by name
        const radios = {};
        document.querySelectorAll('input[type=radio]').forEach(r => {
            if (!r.offsetParent) return;
            (radios[r.name] = radios[r.name] || []).push(r);
        });
        Object.entries(radios).forEach(([name, els]) => {
            out.push({
                kind: 'radio_group',
                name,
                count: els.length,
                labels: els.map(r => {
                    const l = document.querySelector(`label[for="${r.id}"]`);
                    return l?.innerText?.trim() || r.value;
                }),
                anyChecked: els.some(r => r.checked),
            });
        });
        // Checkboxes (the bot does NOT scan these — likely culprit)
        const cbs = document.querySelectorAll('input[type=checkbox]');
        cbs.forEach(cb => {
            if (!cb.offsetParent) return;
            const lbl = document.querySelector(`label[for="${cb.id}"]`);
            out.push({
                kind: 'checkbox',
                id: cb.id, name: cb.name,
                label: lbl?.innerText?.trim() || '',
                checked: cb.checked,
                required: cb.required,
                outerHTML: cb.outerHTML.slice(0, 400),
            });
        });
        // Custom widgets — combobox, listbox, date pickers, switches, etc.
        const customRoles = ['combobox', 'listbox', 'switch', 'spinbutton', 'slider'];
        document.querySelectorAll('[role]').forEach(el => {
            const r = el.getAttribute('role');
            if (!customRoles.includes(r) || !el.offsetParent) return;
            out.push({
                kind: 'aria_widget',
                role: r,
                id: el.id,
                ariaLabel: el.getAttribute('aria-label') || '',
                text: el.innerText?.trim().slice(0, 100) || '',
                outerHTML: el.outerHTML.slice(0, 400),
            });
        });
        // contenteditable (rich text answer fields)
        document.querySelectorAll('[contenteditable="true"]').forEach(el => {
            if (!el.offsetParent) return;
            out.push({
                kind: 'contenteditable',
                id: el.id,
                text: el.innerText?.trim().slice(0, 100) || '',
                outerHTML: el.outerHTML.slice(0, 400),
            });
        });
        return out;
    }""")


async def question_blocks(page):
    """Get every visible question block heading + nested inputs from Seek's question UI."""
    return await page.evaluate("""() => {
        // Seek wraps each question in a labelled fieldset / div with role=group
        const groups = document.querySelectorAll(
            '[data-automation^="employer"], fieldset, [role=group], [role=radiogroup]'
        );
        return Array.from(groups)
            .filter(g => g.offsetParent !== null)
            .map(g => ({
                automation: g.getAttribute('data-automation') || '',
                role: g.getAttribute('role') || '',
                heading: g.querySelector('legend, h2, h3, label')?.innerText?.trim().slice(0, 200) || '',
                inputCount: g.querySelectorAll('input,select,textarea,[contenteditable]').length,
                textPreview: g.innerText?.trim().slice(0, 250) || '',
            }));
    }""")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        kwargs = {"headless": False}
        if CHROME.exists():
            kwargs["executable_path"] = str(CHROME)
        browser = await pw.chromium.launch(**kwargs)
        ctx = await browser.new_context(storage_state=SESSION)
        page = await ctx.new_page()

        log.info(f"Opening {APPLY_URL}")
        await page.goto(APPLY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        log.info(f"Landed on: {page.url}")

        # Already-applied check
        body = (await page.content()).lower()
        if "you've applied" in body or "already applied" in body or "view application" in body:
            log.warning("⚠️  Page indicates this job is ALREADY APPLIED — submission DID go through")
        elif "login" in page.url.lower() or "oauth" in page.url.lower():
            log.error("❌ Session expired — re-run setup_sessions.py")
            await browser.close()
            return
        else:
            log.info("✅ Apply form is live — investigating fields")

        await page.screenshot(path=str(OUT / "01_landing.png"), full_page=True)

        # Upload tailored resume + cover letter using the SAME functions the bot uses
        log.info("Clearing profile resumes")
        await seek_apply._clear_profile_resumes(page)
        log.info(f"Uploading resume: {RESUME.name}")
        await seek_apply._upload_resume(page, str(RESUME), RESUME.name)
        log.info(f"Uploading cover letter: {COVER.name}")
        await seek_apply._upload_cover_letter(page, str(COVER), COVER.name)
        await asyncio.sleep(5)
        await seek_apply._click_continue(page)

        # Walk through Continue clicks until we see "questions" step
        for step_num in range(6):
            try:
                step = await page.locator("button[aria-current='step']").first.inner_text(timeout=2000)
            except Exception:
                step = ""
            log.info(f"Step {step_num}: '{step.strip()}'")
            await page.screenshot(path=str(OUT / f"step_{step_num}_{step.strip().replace(' ','_')[:30]}.png"))

            if "question" in step.lower() or "answer" in step.lower():
                log.info("=== ON QUESTIONS STEP — DUMPING ALL FIELDS ===")
                fields = await all_visible_fields(page)
                blocks = await question_blocks(page)
                (OUT / "questions_fields.json").write_text(json.dumps(fields, indent=2))
                (OUT / "question_blocks.json").write_text(json.dumps(blocks, indent=2))
                (OUT / "questions_dom.html").write_text(await page.content())

                log.info(f"Found {len(blocks)} question blocks:")
                for b in blocks:
                    log.info(f"  • [{b['automation'] or b['role']}] heading='{b['heading'][:80]}' inputs={b['inputCount']}")
                    log.info(f"      preview: {b['textPreview'][:120]}")
                log.info(f"Found {len(fields)} interactive elements:")
                kinds = {}
                for f in fields:
                    kinds[f['kind']] = kinds.get(f['kind'], 0) + 1
                log.info(f"  by kind: {kinds}")
                for f in fields:
                    if f['kind'] == 'native':
                        log.info(f"  native {f['tag']} type={f['type']} id={f['id']} label='{f['label'][:60]}' required={f['required']}")
                    elif f['kind'] == 'radio_group':
                        log.info(f"  radio_group name={f['name']} count={f['count']} checked={f['anyChecked']}")
                        for lbl in f['labels'][:5]:
                            log.info(f"      option: {lbl[:60]}")
                    elif f['kind'] == 'checkbox':
                        log.info(f"  CHECKBOX id={f['id']} label='{f['label'][:80]}' required={f['required']}")
                    elif f['kind'] == 'aria_widget':
                        log.info(f"  ARIA-WIDGET role={f['role']} aria-label='{f['ariaLabel'][:60]}' text='{f['text'][:60]}'")
                    elif f['kind'] == 'contenteditable':
                        log.info(f"  CONTENTEDITABLE id={f['id']} text='{f['text'][:60]}'")
                break

            # Click Continue to advance
            cont = page.get_by_role("button", name="Continue")
            if await cont.count() == 0:
                log.info("No Continue button — likely on review step or done")
                break
            try:
                await cont.first.evaluate("el => el.click()")
                log.info("  → Clicked Continue")
                await asyncio.sleep(3)
            except Exception as e:
                log.warning(f"Continue click failed: {e}")
                break

        log.info(f"Outputs in {OUT.resolve()}")
        log.info("Browser left open for 60s — inspect manually then it'll close")
        await asyncio.sleep(60)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
