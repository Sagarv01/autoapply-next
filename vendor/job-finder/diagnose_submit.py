"""
Walk through Showpad's apply flow with INSTRUMENTATION at every post-submit
moment. We need to see what actually happens after the Submit button click —
modal, redirect, validation error, silent failure?

Captures:
- Screenshots before submit, after submit, +2s, +4s, +8s
- URL transitions
- Any visible buttons/dialogs/alerts after submit
- Final page DOM
"""
import asyncio
import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright

import seek_apply

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("diag")

JOB_URL = "https://au.seek.com/job/91581982"  # Showpad DevOps Engineer
APPLY_URL = JOB_URL + "/apply"
SESSION = str(Path("sessions/seek/state.json").resolve())
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
OUT = Path("errors/diagnose_submit")
RESUME = Path("output/SagarVerma_Showpad_DevOpsEngineer_20260417_230650_525.pdf").resolve()
COVER = Path("output/CoverLetter_Showpad_DevOpsEngineer_20260417_230652_169.pdf").resolve()


async def snapshot(page, label: str):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{label}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception as e:
        log.warning(f"  screenshot {label} failed: {e}")
    log.info(f"  📸 {label}: url={page.url}")
    # Capture buttons + dialogs visible right now
    state = await page.evaluate("""() => {
        const buttons = Array.from(document.querySelectorAll('button, [role=button]'))
            .filter(b => b.offsetParent !== null)
            .map(b => ({
                text: b.innerText?.trim().slice(0, 60) || '',
                aria: b.getAttribute('aria-label') || '',
                automation: b.getAttribute('data-automation') || '',
                disabled: b.disabled || b.getAttribute('aria-disabled') === 'true',
            }))
            .filter(b => b.text || b.aria);
        const dialogs = Array.from(document.querySelectorAll('[role=dialog], [role=alertdialog]'))
            .map(d => ({text: d.innerText?.trim().slice(0, 200), heading: d.querySelector('h1,h2,h3')?.innerText?.trim() || ''}));
        const alerts = Array.from(document.querySelectorAll('[role=alert], [data-automation*="error"]'))
            .filter(e => e.offsetParent !== null)
            .map(e => e.innerText?.trim().slice(0, 200));
        const heading = document.querySelector('h1, h2')?.innerText?.trim() || '';
        return {heading, buttons: buttons.slice(0, 12), dialogs, alerts};
    }""")
    log.info(f"     heading: {state['heading'][:80]!r}")
    if state['dialogs']:
        log.info(f"     ⚠️ DIALOGS: {state['dialogs']}")
    if state['alerts']:
        log.info(f"     ⚠️ ALERTS: {state['alerts']}")
    log.info(f"     buttons: {[b['text'] or b['aria'] for b in state['buttons'] if b['text'] or b['aria']]}")
    (OUT / f"{label}.json").write_text(json.dumps(state, indent=2))


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

        body = (await page.content()).lower()
        if "you've applied" in body or "already applied" in body or "view application" in body:
            log.warning("⚠️  Already applied — exiting.")
            await browser.close()
            return

        # Reuse bot logic for upload + steps
        log.info("Clearing profile resumes")
        await seek_apply._clear_profile_resumes(page)
        log.info("Uploading docs")
        await seek_apply._upload_resume(page, str(RESUME), RESUME.name)
        await seek_apply._upload_cover_letter(page, str(COVER), COVER.name)
        await asyncio.sleep(5)
        await seek_apply._click_continue(page)

        # Walk through up to 5 form steps
        from models import JobListing
        import yaml
        cfg = yaml.safe_load(open("config.yaml"))
        candidate = cfg["candidate"]
        job = JobListing(url=JOB_URL, title="DevOps Engineer", company="Showpad",
                         board="seek", description="", easy_apply=True)

        for i in range(5):
            step = await seek_apply._current_step_text(page)
            log.info(f"Step {i}: '{step}'")
            await snapshot(page, f"step_{i}_{step.replace(' ', '_')[:30]}")
            if "review" in step:
                break
            await seek_apply._handle_form_step(page, job, candidate)
            await seek_apply._click_continue(page)
            await asyncio.sleep(2)

        # Now we're on the review step — instrument the submit
        log.info("\n=== PRE-SUBMIT STATE ===")
        await snapshot(page, "00_pre_submit")

        # Find the submit button
        candidates = ["Submit application", "Submit", "Apply now", "Apply"]
        submit_btn = None
        used_text = None
        for txt in candidates:
            btn = page.get_by_role("button", name=txt, exact=False)
            if await btn.count() > 0:
                submit_btn = btn.first
                used_text = txt
                break
        if submit_btn is None:
            log.error("No submit button found on review page — aborting")
            await asyncio.sleep(60)
            await browser.close()
            return

        log.info(f"\n=== CLICKING SUBMIT ('{used_text}') ===")
        try:
            await submit_btn.scroll_into_view_if_needed()
            await submit_btn.evaluate("el => el.click()")
        except Exception as e:
            log.error(f"Submit click errored: {e}")

        # Capture every 2s for 12s after submit
        for t in (1, 3, 5, 8, 12):
            await asyncio.sleep(t if t == 1 else (t - prev if 'prev' in dir() else 2))
            prev = t
            await snapshot(page, f"01_post_submit_{t}s")

        # Final: navigate to applied jobs and check
        log.info("\n=== CHECKING APPLIED JOBS PAGE ===")
        await page.goto("https://au.seek.com/my-activity/applied-jobs",
                        wait_until="networkidle", timeout=20000)
        await asyncio.sleep(5)
        await snapshot(page, "02_applied_jobs_page")
        body = (await page.content()).lower()
        log.info(f"  showpad in page: {'showpad' in body}")
        log.info(f"  91581982 in page: {'91581982' in body}")
        log.info(f"  devops engineer in page: {'devops engineer' in body}")

        log.info(f"\nAll outputs in {OUT.resolve()}")
        log.info("Browser stays open 60s for inspection")
        await asyncio.sleep(60)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
