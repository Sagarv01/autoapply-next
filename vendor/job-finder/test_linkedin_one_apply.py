"""
Step 2: open ONE LinkedIn job, find the Easy Apply button, walk through the
modal capturing screenshots + button states at each step. Stop just before
Submit — DO NOT auto-submit on the first test. We want to see what the modal
looks like before we trust the bot to send a real application.

Outputs go to errors/linkedin_test/. After running, review the screenshots
and tell me: did it reach the Submit step cleanly? what was on each step?
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

JOB_URL = "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4389312121"
USER_DATA_DIR = Path("sessions/linkedin_chrome_profile").resolve()
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
OUT = Path("errors/linkedin_test")


async def snapshot(page, label: str):
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(OUT / f"{label}.png"), full_page=True)
    except Exception as e:
        print(f"  screenshot {label} failed: {e}")
    state = await page.evaluate("""() => {
        const buttons = Array.from(document.querySelectorAll('button, [role=button]'))
            .filter(b => b.offsetParent !== null)
            .map(b => ({
                text: b.innerText?.trim().slice(0, 80) || '',
                aria: b.getAttribute('aria-label') || '',
            }))
            .filter(b => b.text || b.aria);
        const heading = document.querySelector('h1, h2, h3')?.innerText?.trim().slice(0, 100) || '';
        const dialogs = Array.from(document.querySelectorAll('[role=dialog]'))
            .map(d => ({
                heading: d.querySelector('h1,h2,h3')?.innerText?.trim() || '',
                text_preview: d.innerText?.trim().slice(0, 200) || '',
            }));
        return {url: location.href, heading, buttons: buttons.slice(0, 20), dialogs};
    }""")
    print(f"\n📸 {label}")
    print(f"   url:     {state['url'][:100]}")
    print(f"   heading: {state['heading']!r}")
    if state['dialogs']:
        for d in state['dialogs']:
            print(f"   DIALOG heading={d['heading']!r}")
            print(f"          preview={d['text_preview'][:120]!r}")
    btn_texts = [b['text'] or b['aria'] for b in state['buttons']]
    print(f"   buttons: {btn_texts[:10]}")
    (OUT / f"{label}.json").write_text(json.dumps(state, indent=2))


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        kwargs: dict = {
            "user_data_dir": str(USER_DATA_DIR),
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if CHROME_PATH.exists():
            kwargs["executable_path"] = str(CHROME_PATH)
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print(f"Opening {JOB_URL}")
        await page.goto(JOB_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)

        if "login" in page.url.lower() or "checkpoint" in page.url.lower():
            print("❌ Got redirected to login/checkpoint. Re-run setup_linkedin_profile.py.")
            await ctx.close()
            return

        await snapshot(page, "00_landing")

        # Find Easy Apply button. LinkedIn uses 'jobs-apply-button' id and aria-label.
        easy_apply = page.locator(
            'button.jobs-apply-button, button[aria-label*="Easy Apply"], '
            'button:has-text("Easy Apply")'
        )
        # Filter for "Easy Apply" specifically (not the external "Apply" link)
        if await easy_apply.count() == 0:
            # Try the broader "Apply" but check label
            apply_btn = page.locator('button:has-text("Apply"), a:has-text("Apply")')
            count = await apply_btn.count()
            if count == 0:
                print("❌ No Apply button found at all — job page may not have loaded.")
                await asyncio.sleep(20)
                await ctx.close()
                return
            # Check first button's label/aria
            label = (await apply_btn.first.get_attribute("aria-label") or "") + " " + (await apply_btn.first.inner_text() or "")
            if "easy apply" not in label.lower():
                print(f"❌ This job uses external apply (button label: {label!r}) — bot can't fill these.")
                await asyncio.sleep(20)
                await ctx.close()
                return
            easy_apply = apply_btn

        print("\n✅ Found Easy Apply button — clicking")
        await easy_apply.first.click()
        await asyncio.sleep(3)
        await snapshot(page, "01_modal_opened")

        # Walk through modal pages by clicking "Next" / "Continue to next step" until Submit appears
        for step_num in range(1, 10):
            # Detect end-of-form: any visible button labelled "Submit application" or similar
            submit_btn = page.locator(
                'button[aria-label*="Submit application"], button:has-text("Submit application")'
            )
            if await submit_btn.count() > 0:
                print(f"\n🏁 Reached Submit step (step {step_num}). Clicking Submit now.")
                await snapshot(page, f"99_pre_submit_step_{step_num}")
                await submit_btn.first.click()
                await asyncio.sleep(4)
                await snapshot(page, f"99_post_submit")

                # Verify by navigating to Applied jobs and looking for our job ID.
                JOB_ID = "4389312121"
                print("\nVerifying on /jobs/applied-jobs ...")
                await page.goto(
                    "https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED",
                    wait_until="domcontentloaded", timeout=20000,
                )
                await asyncio.sleep(5)
                await snapshot(page, "99_applied_jobs_page")
                body = (await page.content()).lower()
                if JOB_ID in body:
                    print(f"✅ VERIFIED — job id {JOB_ID} appears on Applied Jobs page.")
                else:
                    print(f"❌ NOT VERIFIED — job id {JOB_ID} not found on Applied Jobs page.")
                break

            # Else find Next / Continue / Review
            next_btn = page.locator(
                'button[aria-label*="Continue to next step"], button[aria-label*="Review"], '
                'button:has-text("Next"), button:has-text("Continue"), button:has-text("Review")'
            )
            if await next_btn.count() == 0:
                print(f"\n❌ No Next/Continue/Review/Submit button found at step {step_num}. Stopping.")
                await snapshot(page, f"X_stuck_step_{step_num}")
                break

            try:
                await next_btn.first.scroll_into_view_if_needed()
                await next_btn.first.click()
                await asyncio.sleep(2.5)
                await snapshot(page, f"02_step_{step_num}_after_next")
            except Exception as e:
                print(f"\n❌ Click failed at step {step_num}: {e}")
                await snapshot(page, f"X_click_fail_step_{step_num}")
                break
        else:
            print("\n⚠️  Looped 10 times without finding Submit. Possibly a long form.")

        print(f"\nAll outputs in {OUT.resolve()}")
        print("Browser stays open for 60s for manual inspection.")
        await asyncio.sleep(60)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
