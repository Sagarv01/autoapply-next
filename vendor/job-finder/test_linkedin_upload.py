"""
Step 2b: same as test_linkedin_one_apply.py BUT upload OUR tailored resume
(via 'Upload resume' button → set_input_files on the hidden file input)
before clicking Submit. Verify on Applied tab.
"""
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY missing in .env"

from playwright.async_api import async_playwright
from models import JobListing
from tailorer import tailor
from seek_apply import _claude_answer

CLAUDE_MODEL = "claude-haiku-4-5"  # cheap model for screening Q answers

JOB_ID = "4391026869"
JOB_URL = f"https://www.linkedin.com/jobs/view/{JOB_ID}/"
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
        const buttons = Array.from(document.querySelectorAll('button'))
            .filter(b => b.offsetParent !== null)
            .map(b => (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 80))
            .filter(t => t);
        const dialog = document.querySelector('[role=dialog]');
        return {url: location.href, buttons: buttons.slice(0, 12),
                dialog_text: dialog?.innerText?.trim().slice(0, 250) || ''};
    }""")
    print(f"\n📸 {label}  url={state['url'][:90]}")
    if state['dialog_text']:
        print(f"   dialog: {state['dialog_text'][:200]!r}")
    print(f"   buttons: {state['buttons']}")
    (OUT / f"{label}.json").write_text(json.dumps(state, indent=2))


async def scrape_modal_questions(page) -> list[dict]:
    """Find every visible input/select/radio-group/checkbox across all frames
    (main + iframes — LinkedIn Easy Apply often loads form in iframe)."""
    all_fields = []
    combined_stats = {"total_inputs": 0, "after_chrome": 0, "after_vis": 0,
                      "total_radios": 0, "total_checkboxes": 0}
    # Include main frame + all sub-frames
    frames = page.frames
    for frame in frames:
        try:
            res = await _scrape_one_frame(frame)
        except Exception as e:
            continue
        all_fields.extend(res["fields"])
        for k in combined_stats:
            combined_stats[k] += res["stats"].get(k, 0)
    print(f"   📋 scrape stats (all frames, {len(frames)} total): {combined_stats}")
    if all_fields:
        print(f"   📋 fields found:")
        for f in all_fields[:10]:
            print(f"      [{f['kind']}] label={f['label'][:80]!r} value={f['value'][:40]!r}")
    return all_fields


async def _scrape_one_frame(frame) -> dict:
    # Targeted LinkedIn scrape: every Easy Apply form field is wrapped in
    # `[data-test-form-element]`. Use that as the entry point — much more
    # reliable than generic input querying.
    return await frame.evaluate("""() => {
        const out = [];
        const stats = {total_form_elements: 0, kept: 0,
                       total_inputs: 0, after_chrome: 0, after_vis: 0,
                       total_radios: 0, total_checkboxes: 0};
        document.querySelectorAll('[data-test-form-element]').forEach(el => {
            stats.total_form_elements++;
            // Find the actual interactive widget inside
            const select = el.querySelector('select');
            const txt = el.querySelector('input[type=text], input[type=number], input[type=email], input:not([type]), textarea');
            const radios = el.querySelectorAll('input[type=radio]');
            const cbs = el.querySelectorAll('input[type=checkbox]');
            const labelEl = el.querySelector('label, legend, .fb-dash-form-element__label');
            const label = (labelEl?.innerText || '').trim().slice(0, 250);
            if (select) {
                out.push({
                    label, kind: 'select',
                    field_id: select.id || select.name || '',
                    options: Array.from(select.options).map(o => o.text.trim()).filter(t => t && !/^select an option$/i.test(t)),
                    value: select.value === 'Select an option' ? '' : (select.value || ''),
                });
                stats.kept++;
            } else if (radios.length > 0) {
                out.push({
                    label, kind: 'radio_group',
                    field_id: radios[0].name || radios[0].id,
                    options: Array.from(radios).map(r => {
                        const l = document.querySelector(`label[for="${r.id.replace(/(["\\\\])/g, '\\\\$1')}"]`);
                        return (l?.innerText || r.value).trim();
                    }),
                    value: Array.from(radios).find(r => r.checked)?.value || '',
                });
                stats.kept++;
                stats.total_radios += radios.length;
            } else if (cbs.length > 0) {
                out.push({
                    label, kind: 'checkbox_group',
                    field_id: cbs[0].name || cbs[0].id,
                    options: Array.from(cbs).map(c => {
                        const l = document.querySelector(`label[for="${c.id.replace(/(["\\\\])/g, '\\\\$1')}"]`);
                        return (l?.innerText || c.value).trim();
                    }),
                    value: Array.from(cbs).find(c => c.checked)?.value || '',
                });
                stats.kept++;
                stats.total_checkboxes += cbs.length;
            } else if (txt) {
                out.push({
                    label, kind: txt.tagName.toLowerCase() + ':' + (txt.type || 'text'),
                    field_id: txt.id || txt.name || '',
                    options: [],
                    value: txt.value || '',
                });
                stats.kept++;
                stats.total_inputs++;
            }
        });
        return {fields: out, stats};
    }""")
    # Legacy fallback (for forms without [data-test-form-element]) — not run
    return await frame.evaluate("""() => {
        const dlg = document;
        const out = [];
        const seen = new Set();
        const stats = {total_inputs: 0, after_chrome: 0, after_vis: 0,
                       total_radios: 0, total_checkboxes: 0};
        // Filter: skip fields inside the page chrome
        const inChrome = el => !!el.closest('header, nav, footer, [role=navigation], [role=banner], [role=contentinfo]');

        // Native inputs (text/number/email) — DROP all filters for now,
        // we'll filter in Python by label content instead.
        dlg.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]),select,textarea').forEach(el => {
            stats.total_inputs++;
            // Skip clearly chrome things (search inputs, login fields)
            const t = (el.type || '').toLowerCase();
            const placeholder = (el.placeholder || '').toLowerCase();
            if (t === 'search' || placeholder.includes('search')) return;
            stats.after_chrome++;
            // Use a permissive visibility check: width or height > 0
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return;
            stats.after_vis++;
            if (seen.has(el.id || el.name)) return;
            seen.add(el.id || el.name);
            const lbl = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
            const fb = el.closest('div, fieldset')?.querySelector('label, legend');
            const text = (lbl?.innerText || fb?.innerText || '').trim();
            out.push({
                label: text.slice(0, 250),
                kind: el.tagName.toLowerCase() + (el.type ? ':' + el.type : ''),
                field_id: el.id || el.name || '',
                options: el.tagName === 'SELECT'
                    ? Array.from(el.options).map(o => o.text).filter(t => t && !/^select/i.test(t))
                    : [],
                value: el.value || '',
            });
        });

        // Radio groups (deduped by name)
        const radioNames = new Set();
        dlg.querySelectorAll('input[type=radio]').forEach(r => {
            if (r.getClientRects().length === 0) return;
            if (inChrome(r)) return;
            radioNames.add(r.name);
        });
        radioNames.forEach(name => {
            if (!name) return;
            const safe = name.replace(/(["\\\\])/g, '\\\\$1');
            const radios = Array.from(dlg.querySelectorAll(`input[type=radio][name="${safe}"]`));
            if (!radios.length) return;
            const fieldset = radios[0].closest('fieldset');
            const label = (fieldset?.querySelector('legend')?.innerText
                        || radios[0].closest('div')?.querySelector('label')?.innerText
                        || '').trim();
            out.push({
                label: label.slice(0, 250),
                kind: 'radio_group',
                field_id: name,
                options: radios.map(r => {
                    const l = document.querySelector(`label[for="${r.id}"]`);
                    return (l?.innerText || r.value).trim();
                }),
                value: radios.find(r => r.checked)?.value || '',
            });
        });

        // Checkbox groups (deduped by name) — needed for "On Site / Hybrid / Remote"
        // style multi-select questions LinkedIn uses.
        const cbNames = new Set();
        dlg.querySelectorAll('input[type=checkbox]').forEach(c => {
            if (c.getClientRects().length === 0) return;
            if (inChrome(c)) return;
            if (c.name) cbNames.add(c.name);
        });
        cbNames.forEach(name => {
            const safe = name.replace(/(["\\\\])/g, '\\\\$1');
            const cbs = Array.from(dlg.querySelectorAll(`input[type=checkbox][name="${safe}"]`));
            if (!cbs.length) return;
            const fs = cbs[0].closest('fieldset');
            const label = (fs?.querySelector('legend')?.innerText
                        || cbs[0].closest('div')?.querySelector('label, legend')?.innerText
                        || '').trim();
            out.push({
                label: label.slice(0, 250),
                kind: 'checkbox_group',
                field_id: name,
                options: cbs.map(c => {
                    const l = document.querySelector(`label[for="${c.id}"]`);
                    return (l?.innerText || c.value).trim();
                }),
                value: cbs.find(c => c.checked)?.value || '',
            });
        });
        // Add radio + checkbox counts
        dlg.querySelectorAll('input[type=radio]').forEach(_ => stats.total_radios++);
        dlg.querySelectorAll('input[type=checkbox]').forEach(_ => stats.total_checkboxes++);
        return {fields: out, stats};
    }""")


async def _apply_radio_or_select(page, q: dict, answer: str) -> bool:
    """Apply an answer to a radio_group / select / text field. Returns True on success."""
    fid = q["field_id"]
    try:
        if q["kind"].startswith("select"):
            best = None
            for opt in q["options"]:
                if answer.lower() in opt.lower() or opt.lower() in answer.lower():
                    best = opt
                    break
            await page.locator(f'[id="{fid}"]').select_option(label=best or q["options"][0])
            return True
        if q["kind"] == "checkbox_group":
            # Multi-select. Pick the option whose label matches the answer best.
            cbs = await page.evaluate(
                """(name) => Array.from(document.querySelectorAll(`input[type=checkbox][name="${name}"]`)).map(c => ({
                    id: c.id,
                    label: (document.querySelector(`label[for="${c.id}"]`)?.innerText || c.value).trim()
                }))""", fid,
            )
            target_id = None
            for c in cbs:
                if answer.lower() in c["label"].lower() or c["label"].lower() in answer.lower():
                    target_id = c["id"]
                    break
            if not target_id and cbs:
                target_id = cbs[0]["id"]  # fallback
            if target_id:
                lbl = page.locator(f'label[for="{target_id}"]')
                if await lbl.count():
                    await lbl.first.click(timeout=5000)
                else:
                    await page.locator(f'[id="{target_id}"]').check(force=True, timeout=5000)
                return True
        if q["kind"] == "radio_group":
            radios = await page.evaluate(
                """(name) => Array.from(document.querySelectorAll(`input[type=radio][name="${name}"]`)).map(r => ({
                    id: r.id,
                    label: (document.querySelector(`label[for="${r.id}"]`)?.innerText || r.value).trim()
                }))""", fid,
            )
            target_id = None
            for r in radios:
                if answer.lower() in r["label"].lower() or r["label"].lower() in answer.lower():
                    target_id = r["id"]
                    break
            if not target_id and radios:
                target_id = radios[0]["id"]
            if target_id:
                lbl = page.locator(f'label[for="{target_id}"]')
                if await lbl.count():
                    await lbl.first.click(timeout=5000)
                else:
                    await page.locator(f'[id="{target_id}"]').check(force=True, timeout=5000)
                return True
        else:
            sel = f'[id="{fid}"]' if fid else ''
            if sel:
                await page.locator(sel).fill(answer, timeout=5000)
                return True
    except Exception as e:
        print(f"   ❌ apply failed: {e}")
    return False


async def fill_question(page, q: dict, job: JobListing) -> bool:
    """Ask Claude (Haiku) to answer one question and apply the answer."""
    if q["value"] and q["kind"] != "radio_group":
        return False
    if q["kind"] == "radio_group" and q["value"]:
        return False

    label_lower = q["label"].lower()

    # ── Hard-rule answers (don't trust the LLM on these) ──
    if any(kw in label_lower for kw in ("sponsorship", "sponsor for", "require sponsorship",
                                         "visa sponsorship")):
        answer = "No"
        print(f"   Q: {q['label'][:80]!r}\n   → 'No' (hard rule: visa sponsorship not needed)")
        return await _apply_radio_or_select(page, q, answer)

    # Location (city)
    if "location (city)" in label_lower or label_lower.strip() in ("city", "location"):
        answer = "Sydney, New South Wales, Australia"
        print(f"   Q: {q['label'][:80]!r}\n   → {answer!r} (hard rule)")
        return await _apply_radio_or_select(page, q, answer)

    # Working rights: candidate has full work rights via 485 visa
    if any(kw in label_lower for kw in ("working rights", "work rights", "right to work",
                                         "work authoris", "work authoriz")):
        answer = "Yes" if q["kind"] in ("radio_group", "checkbox_group") else \
                 "Yes, I have full working rights in Australia (485 visa, valid through July 2028)"
        # For selects, look for the most permissive option
        if q["kind"].startswith("select"):
            for opt in q["options"]:
                if any(k in opt.lower() for k in ("citizen", "permanent resident", "work visa",
                                                   "no restriction", "full work", "australian")):
                    answer = opt
                    break
        print(f"   Q: {q['label'][:80]!r}\n   → {answer!r} (hard rule: full work rights)")
        return await _apply_radio_or_select(page, q, answer)

    # Work environment preference: pick whatever the job listing offers (Hybrid is safest default)
    if any(kw in label_lower for kw in ("work environment", "work setting", "work arrangement",
                                         "remote/hybrid", "onsite/hybrid")):
        # Pick the option matching the job's listed setup, fallback to Hybrid
        for opt in q["options"]:
            if "hybrid" in opt.lower():
                print(f"   Q: {q['label'][:80]!r}\n   → {opt!r} (hard rule: hybrid)")
                return await _apply_radio_or_select(page, q, opt)

    # Notice period: candidate uses 2 weeks
    if any(kw in label_lower for kw in ("notice period", "notice are you required",
                                         "notice to give")):
        if q["kind"].startswith("select"):
            for opt in q["options"]:
                if "2 week" in opt.lower() or "two week" in opt.lower() or "1-2 week" in opt.lower():
                    print(f"   Q: {q['label'][:80]!r}\n   → {opt!r} (hard rule: 2 weeks notice)")
                    return await _apply_radio_or_select(page, q, opt)
        print(f"   Q: {q['label'][:80]!r}\n   → '2 weeks' (hard rule)")
        return await _apply_radio_or_select(page, q, "2 weeks")

    # Salary expectation: 110000 (matches existing Seek logic)
    if any(kw in label_lower for kw in ("salary expectation", "expected salary",
                                         "salary you", "remuneration")):
        print(f"   Q: {q['label'][:80]!r}\n   → '110000' (hard rule: market rate)")
        return await _apply_radio_or_select(page, q, "110000")

    # Holidays in next N months: No (no plans booked)
    if any(kw in label_lower for kw in ("holidays", "vacation booked", "leave booked",
                                         "annual leave booked")):
        print(f"   Q: {q['label'][:80]!r}\n   → 'No' (hard rule: no holidays booked)")
        return await _apply_radio_or_select(page, q, "No")

    # If it's a number-typed input or the label asks "How many...", request just a digit
    is_numeric = q["kind"] == "input:number" or any(
        kw in label_lower for kw in ("how many years", "years of experience", "years experience",
                                      "number of", "how many ")
    )
    question = q["label"]
    if is_numeric:
        question = (
            f"{q['label']}\n"
            "REPLY WITH ONLY A SINGLE NUMBER (no words, no units). "
            "Pick a number consistent with the candidate resume; if none in resume, answer 0."
        )

    answer = (await _claude_answer(question, q["options"] or None, job, model=CLAUDE_MODEL)).strip()

    if is_numeric:
        # Strip everything except the first integer
        import re
        m = re.search(r"\d+", answer)
        answer = m.group(0) if m else "0"

    print(f"   Q: {q['label'][:80]!r}\n   → {answer!r}")
    return await _apply_radio_or_select(page, q, answer)


async def scrape_job_meta(page):
    """Get job title + company + description from the LinkedIn job page.
    Falls back to <title> tag if React panel selectors miss."""
    meta = await page.evaluate("""() => {
        const pickText = sels => {
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
            }
            return '';
        };
        const title = pickText([
            '.job-details-jobs-unified-top-card__job-title h1',
            '.job-details-jobs-unified-top-card__job-title',
            '.t-24.job-details-jobs-unified-top-card__job-title',
            'h1',
        ]);
        const company = pickText([
            '.job-details-jobs-unified-top-card__company-name a',
            '.job-details-jobs-unified-top-card__company-name',
            '[class*="company-name"] a',
            '[class*="company-name"]',
        ]);
        let desc = pickText([
            '.jobs-description__content .jobs-box__html-content',
            '.jobs-description__content',
            '.jobs-box__html-content',
            '[class*="jobs-description"]',
            'article.jobs-description',
            '#job-details',
            '[class*="description__container"]',
        ]);
        // Fallback: find the largest text block under main containing "About the job"
        // or the longest text block on the page if all selectors miss.
        if (!desc || desc.length < 200) {
            const candidates = Array.from(document.querySelectorAll('main *, article *'))
                .filter(el => el.children.length === 0 || el.tagName === 'DIV')
                .map(el => el.innerText || '')
                .filter(t => t.length > 200);
            candidates.sort((a, b) => b.length - a.length);
            if (candidates.length) desc = candidates[0];
        }
        desc = (desc || '').slice(0, 8000);
        return {title, company, desc, page_title: document.title};
    }""")
    # Cleanup: slice from 'About the job' to the first '… more' / 'Show more'
    # to drop LinkedIn chrome (Premium upsells, footer, related jobs, etc.).
    raw = meta.get("desc") or ""
    if raw:
        start_idx = raw.lower().find("about the job")
        if start_idx >= 0:
            raw = raw[start_idx:]
        # Cut at first end-of-description marker
        for marker in ("… more", "...more", "Set alert for similar jobs",
                       "Requirements added by the job poster", "About the company"):
            i = raw.find(marker)
            if i > 0:
                raw = raw[:i]
                break
        meta["desc"] = raw.strip()
    # Fallback: parse "<title>Job Title - Company | LinkedIn</title>"
    if not meta["title"] and meta.get("page_title"):
        pt = meta["page_title"].replace(" | LinkedIn", "")
        if " - " in pt:
            t, c = pt.rsplit(" - ", 1)
            meta["title"] = t.strip()
            if not meta["company"]:
                meta["company"] = c.strip()
        else:
            meta["title"] = pt.strip()
    return meta


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
        await page.goto(JOB_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(6)
        if "login" in page.url.lower() or "checkpoint" in page.url.lower():
            print("❌ Got redirected to login. Re-run setup_linkedin_profile.py.")
            await ctx.close()
            return

        # ── Scrape job meta + tailor PDFs ──
        meta = await scrape_job_meta(page)
        print(f"\nScraped: title={meta['title']!r} company={meta['company']!r} desc_len={len(meta['desc'])}")
        if not meta["title"]:
            print("❌ Couldn't scrape job title — aborting.")
            await ctx.close()
            return

        job = JobListing(
            url=JOB_URL,
            title=meta["title"], company=meta["company"] or "Unknown",
            board="linkedin", description=meta["desc"], easy_apply=True,
        )
        print(f"\nTailoring full-tier (Sonnet) for {job.title} @ {job.company} ...")
        resume_pdf, cover_pdf = await tailor(job, tier="full")
        print(f"✅ Tailored: {Path(resume_pdf).name}")

        # ── Click Easy Apply ──
        await snapshot(page, "u00_landing")
        easy_apply = page.locator(
            'button.jobs-apply-button, button[aria-label*="Easy Apply"], '
            'button:has-text("Easy Apply")'
        )
        if await easy_apply.count() == 0:
            print("❌ No Easy Apply button — this job may use external apply.")
            await ctx.close()
            return
        await easy_apply.first.click()
        await asyncio.sleep(3)
        await snapshot(page, "u01_modal_opened")

        # ── Walk modal: at each step look for (1) file input to upload to,
        # (2) Submit button to click, (3) Next button to advance, in that order
        uploaded = False
        for step_num in range(1, 10):
            await snapshot(page, f"u02_step_{step_num}")

            # 1. File input present + we haven't uploaded yet → upload
            file_input = page.locator('input[type=file]')
            if not uploaded and await file_input.count() > 0:
                print(f"\n📎 Step {step_num}: file input found → uploading {Path(resume_pdf).name}")
                await file_input.first.set_input_files(resume_pdf)
                uploaded = True
                await asyncio.sleep(5)
                await snapshot(page, f"u02_step_{step_num}_after_upload")
                # Don't click Next yet — let the same step settle
                await asyncio.sleep(2)

            # 2. Submit button present → click it
            submit_btn = page.locator(
                'button[aria-label*="Submit application"], button:has-text("Submit application")'
            )
            if await submit_btn.count() > 0:
                if not uploaded:
                    print(f"\n⚠️  Submit reached but never uploaded our resume — using LinkedIn default.")
                print(f"\n🏁 Submit reachable at step {step_num}. Clicking.")
                await snapshot(page, f"u99_pre_submit_step_{step_num}")
                await submit_btn.first.click()
                await asyncio.sleep(4)
                await snapshot(page, "u99_post_submit")

                print("\nVerifying on /my-items/saved-jobs?cardType=APPLIED ...")
                await page.goto(
                    "https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED",
                    wait_until="domcontentloaded", timeout=20000,
                )
                await asyncio.sleep(5)
                await snapshot(page, "u99_applied_jobs_page")
                body = (await page.content()).lower()
                if JOB_ID in body:
                    print(f"✅ VERIFIED — {JOB_ID} on Applied Jobs page.")
                else:
                    print(f"❌ NOT VERIFIED — {JOB_ID} not found.")
                break

            # 3. Answer any questions on this step using Claude Haiku
            questions = await scrape_modal_questions(page)
            if questions:
                print(f"\n📝 Step {step_num}: {len(questions)} question(s) to answer")
                filled = 0
                for q in questions:
                    if await fill_question(page, q, job):
                        filled += 1
                print(f"   filled {filled}/{len(questions)} fields")
                await asyncio.sleep(1.5)
                await snapshot(page, f"u02_step_{step_num}_after_answers")

            # 4. Next/Review/Continue → advance
            next_btn = page.locator(
                'button[aria-label*="Continue to next step"], button[aria-label*="Review"], '
                'button:has-text("Next"), button:has-text("Continue"), button:has-text("Review")'
            )
            if await next_btn.count() == 0:
                print(f"\n❌ No Next/Submit at step {step_num} (and no file input). Stuck.")
                await snapshot(page, f"uX_stuck_step_{step_num}")
                break
            await next_btn.first.scroll_into_view_if_needed()
            await next_btn.first.click()
            await asyncio.sleep(2.5)
        else:
            print("\n⚠️  10 iterations without Submit.")

        print(f"\nOutputs in {OUT.resolve()}")
        await asyncio.sleep(60)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
