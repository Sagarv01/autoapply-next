"""
Karpathy-minimum LinkedIn end-to-end:
  1. Search LinkedIn for AWS jobs (Easy Apply filter only)
  2. Take first N results
  3. For each: open → scrape → score (GPT-4o-mini) →
       score >= 65 → tailor + apply (full modal walk with question answering)
       score <  65 → skip, log to DB with score
  4. Persist outcome per job to applications table (board=linkedin)

Success: N jobs processed, DB rows show action+score for each.
"""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY missing"
assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY missing"

from playwright.async_api import async_playwright

import tracker
from matcher import score_job
from tailorer import tailor
from models import Application, JobListing
from test_linkedin_upload import scrape_job_meta
from linkedin_a11y_apply import walk_and_apply, verify_applied_on_tracker

USER_DATA_DIR = Path("sessions/linkedin_chrome_profile").resolve()
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
# Listing page: /jobs/search/ (renders the 25 result cards as anchors we can scrape)
LISTING_URL = "https://www.linkedin.com/jobs/search/?keywords=AWS&f_AL=true"
# Per-job navigation: /jobs/search-results/?currentJobId= (keeps Easy Apply visible)
NAV_URL_BASE = "https://www.linkedin.com/jobs/search-results/?keywords=AWS&f_AL=true"
SEARCH_URL = LISTING_URL  # backwards-compat for any other refs
LIMIT = 30  # max jobs to consider in one run
TARGET_APPLIES = 4  # stop after this many real applies
MIN_SCORE_TO_APPLY = 50


async def get_search_results(page, limit: int) -> list[dict]:
    """Scrape up to `limit` unique job IDs from the LinkedIn AWS search,
    paginating via &start=N (25 results per page)."""
    seen = set()
    out = []
    for start in range(0, 200, 25):  # up to 8 pages
        if len(out) >= limit:
            break
        url = LISTING_URL + f"&start={start}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector('a[href*="/jobs/view/"]', timeout=20000)
        except Exception:
            print(f"   ⚠️ start={start}: no job links rendered, stopping pagination")
            break
        await asyncio.sleep(2.5)
        page_results = await page.evaluate("""(base) => {
            const r = [];
            const seen = new Set();
            document.querySelectorAll('a[href*="/jobs/view/"], a[href*="currentJobId="]').forEach(a => {
                const href = a.getAttribute('href') || '';
                let id = null;
                const m1 = href.match(/\\/jobs\\/view\\/(\\d+)/);
                const m2 = href.match(/currentJobId=(\\d+)/);
                id = (m1 && m1[1]) || (m2 && m2[1]);
                if (!id || seen.has(id)) return;
                seen.add(id);
                // Use search-results URL — keeps Easy Apply button visible inline
                r.push({id, url: base + '&currentJobId=' + id});
            });
            return r;
        }""", NAV_URL_BASE)
        new_count = 0
        for r in page_results:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(r)
            new_count += 1
        print(f"   page start={start}: {len(page_results)} found, {new_count} new (total: {len(out)})")
        if new_count == 0:
            break
    return out[:limit]


async def apply_easy_apply(page, job: JobListing, resume_pdf: str,
                            cover_pdf: str | None = None) -> str:
    """Delegate to a11y-based walker (the version verified end-to-end on Cuscal).
    Returns 'applied' / 'no_easy_apply' / 'already_applied' / 'stuck' / 'max_steps'."""
    import yaml
    cfg = yaml.safe_load(open("config.yaml"))
    return await walk_and_apply(page, job, resume_pdf, cover_pdf, cfg["candidate"])


async def _legacy_apply_easy_apply_DELETED(page, job: JobListing, resume_pdf: str,
                                            cover_pdf: str | None = None) -> str:
    """Old broken version — kept commented for reference. Do not call.
    Returns 'applied' | 'no_easy_apply' | 'already_applied' | 'stuck' | 'max_steps'"""
    # Find a button whose accessible name (aria-label or visible text) contains
    # "Easy Apply" — exact and idiomatic. Wait up to 8s for it to render.
    # Easy Apply is sometimes a <button>, sometimes an <a> link (newer layout).
    # Match both via a single selector targeting either tag with that aria-label.
    easy = page.locator(
        'button[aria-label*="Easy Apply"], a[aria-label*="Easy Apply"]'
    ).first
    try:
        await easy.wait_for(timeout=15000)
    except Exception:
        return "no_easy_apply"
    txt = (await easy.inner_text() or "").lower()
    if "applied" in txt and "easy apply" not in txt:
        return "already_applied"
    await easy.click()
    await asyncio.sleep(3)

    handled_file_ids: set[str] = set()
    for step in range(1, 12):
        # Per-step diagnostic snapshot — so we can debug stuck modals
        try:
            from pathlib import Path
            Path("errors/linkedin_test").mkdir(parents=True, exist_ok=True)
            jid = job.url.rstrip("/").split("/")[-1]
            snap_path = f"errors/linkedin_test/apply_{jid}_step_{step}.png"
            await page.screenshot(path=snap_path, full_page=True)
            # Diagnostic: find any visible heading containing 'Apply to'
            dlg_text = await page.evaluate(
                """() => {
                    const head = Array.from(document.querySelectorAll('h1,h2,h3'))
                        .find(h => h.offsetParent !== null && (h.innerText || '').includes('Apply to'));
                    if (!head) return '';
                    // Walk up to find the modal container
                    let p = head;
                    for (let i = 0; i < 10 && p; i++) {
                        if ((p.innerText || '').length > 200) {
                            return p.innerText.trim().slice(0, 250);
                        }
                        p = p.parentElement;
                    }
                    return head.innerText.trim();
                }"""
            )
            # Also count form-element containers via Playwright (cross-frame friendly)
            n_form_elements = await page.locator('[data-test-form-element]').count()
            n_selects = await page.locator('[data-test-form-element] select').count()
            n_radios = await page.locator('[data-test-form-element] input[type=radio]').count()
            print(f"   step {step}: url={page.url[:80]!r} form-els={n_form_elements} selects={n_selects} radios={n_radios}")
        except Exception:
            pass

        # 1. Upload to EVERY file input on this step (resume + cover letter).
        #    Use Playwright locators (which find the inputs reliably) and
        #    inspect each per-element to decide which file to upload.
        n_files = await page.locator('input[type=file]').count()
        for i in range(n_files):
            if i in handled_file_ids:
                continue
            el = page.locator('input[type=file]').nth(i)
            try:
                label = (await el.evaluate(
                    """el => {
                        // Walk up looking for a heading/label that says 'cover' or 'resume'
                        let p = el.parentElement;
                        for (let k = 0; k < 6 && p; k++) {
                            const txt = (p.innerText || '').toLowerCase();
                            if (txt.includes('cover')) return 'cover';
                            if (txt.includes('resume')) return 'resume';
                            p = p.parentElement;
                        }
                        return '';
                    }"""
                )) or ""
                target = cover_pdf if (label == "cover" and cover_pdf) else resume_pdf
                await el.set_input_files(target)
                handled_file_ids.add(i)
                print(f"   📎 uploaded {Path(target).name} → input #{i} (label={label!r})")
            except Exception as e:
                print(f"   ⚠️ upload #{i} failed: {e}")
                handled_file_ids.add(i)  # don't retry forever
        if n_files and handled_file_ids:
            await asyncio.sleep(4)

        # 2. Submit reachable?
        sub = page.locator('button[aria-label*="Submit application"]')
        if await sub.count() > 0:
            print(f"   🏁 clicking Submit")
            await sub.first.click()
            await asyncio.sleep(4)
            return "applied"

        # 3. Answer any questions on this step
        qs = await scrape_modal_questions(page)
        for q in qs:
            try:
                await fill_question(page, q, job)
            except Exception as e:
                print(f"     fill_question crashed for {q['label'][:60]!r}: {e}")

        # 4. Next/Review/Continue → advance
        nxt = page.locator(
            'button[aria-label*="Continue to next step"], button[aria-label*="Review"], '
            'button:has-text("Next"), button:has-text("Continue"), button:has-text("Review")'
        )
        if await nxt.count() == 0:
            return "stuck"
        try:
            await nxt.first.click(timeout=5000)
        except Exception:
            return "stuck"
        await asyncio.sleep(2.5)
    return "max_steps"


async def main():
    tracker.init_db()
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

        print(f"\nSearching LinkedIn for 'AWS' (Easy Apply, default relevance sort)...")
        results = await get_search_results(page, LIMIT)
        print(f"Got {len(results)} job links.\n")

        summary = {"applied": 0, "skipped_low_match": 0, "no_easy_apply": 0,
                   "stuck": 0, "max_steps": 0, "already_applied": 0, "scrape_failed": 0}

        for i, r in enumerate(results, 1):
            print(f"[{i}/{len(results)}] {r['url']}")
            try:
                await page.goto(r["url"], wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_selector(
                        '.job-details-jobs-unified-top-card__job-title, '
                        '.jobs-unified-top-card__job-title, h1, h2', timeout=20000
                    )
                except Exception:
                    pass
                await asyncio.sleep(4)
                # Click "...more" to expand the truncated job description before scraping
                more_btn = page.locator(
                    'button.show-more-less-html__button, '
                    'button[aria-label*="see more"], button[aria-label*="Show more"], '
                    'button.jobs-description__footer-button'
                )
                if await more_btn.count() > 0:
                    try:
                        await more_btn.first.click(timeout=3000)
                        await asyncio.sleep(1.5)
                    except Exception:
                        pass
                meta = await scrape_job_meta(page)
                if not meta["title"]:
                    print("   ❌ scrape failed — skipping")
                    summary["scrape_failed"] += 1
                    continue

                # Show what we scraped (and save full to file for inspection)
                desc = meta.get("desc") or ""
                print(f"   📄 desc {len(desc)} chars — preview: {desc[:300]!r}...")
                Path("errors/linkedin_test").mkdir(parents=True, exist_ok=True)
                Path(f"errors/linkedin_test/jd_{r['id']}.txt").write_text(
                    f"TITLE: {meta['title']}\nCOMPANY: {meta.get('company', '')}\nURL: {r['url']}\n\n{desc}"
                )

                job = JobListing(
                    url=r["url"], title=meta["title"],
                    company=meta.get("company") or "Unknown",
                    board="linkedin", description=meta.get("desc") or "",
                    easy_apply=True,
                )
                score, reasoning = await score_job(job)
                print(f"   📊 {score}% match — {meta['title']!r} @ {job.company!r}")

                app = Application(
                    url=r["url"], title=meta["title"], company=job.company,
                    board="linkedin", match_score=score, match_reasoning=reasoning,
                )

                if score >= MIN_SCORE_TO_APPLY:
                    print(f"   → tailoring (full Sonnet) and applying ...")
                    resume_pdf, cover_pdf = await tailor(job, tier="full")
                    app.resume_file = Path(resume_pdf).name
                    app.cover_letter_file = Path(cover_pdf).name
                    outcome = await apply_easy_apply(page, job, resume_pdf, cover_pdf)
                    if outcome == "applied":
                        # Verify on LinkedIn's official Applied tracker
                        print(f"   → verifying on /jobs-tracker/?stage=applied ...")
                        verified = await verify_applied_on_tracker(page, r["id"])
                        if verified:
                            app.status = "applied"
                            print(f"   ✅ applied & VERIFIED on tracker")
                        else:
                            # FALSE POSITIVE — bot says applied but tracker doesn't show it
                            app.status = "failed"
                            app.notes = "FALSE_POSITIVE: bot returned 'applied' but jobId not on /jobs-tracker"
                            print(f"   ❌ FALSE POSITIVE — saving page for debug")
                            try:
                                await page.screenshot(
                                    path=f"errors/linkedin_test/false_positive_{r['id']}.png",
                                    full_page=True,
                                )
                            except Exception:
                                pass
                            outcome = "false_positive"
                    else:
                        app.status = "failed"
                        app.notes = outcome
                        print(f"   ❌ {outcome}")
                    summary[outcome] = summary.get(outcome, 0) + 1
                else:
                    app.status = "skipped"
                    app.notes = f"Match {score}% < {MIN_SCORE_TO_APPLY}% threshold"
                    print(f"   ⊘ skipped (score < {MIN_SCORE_TO_APPLY}%)")
                    summary["skipped_low_match"] += 1

                tracker.upsert_application(app)

                # Stop early once we've reached the target apply count
                if summary["applied"] >= TARGET_APPLIES:
                    print(f"\n🎯 Reached target {TARGET_APPLIES} applies — stopping early.")
                    break
            except Exception as e:
                print(f"   ❌ unhandled: {type(e).__name__}: {str(e)[:200]}")

        print(f"\n=== SUMMARY ===")
        for k, v in summary.items():
            if v:
                print(f"  {k}: {v}")
        print("\nBrowser stays open for 30s.")
        await asyncio.sleep(30)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
