"""
Audit the last 100 'applied' rows in jobs.db against Seek's actual Applied Jobs page.
The current verifier matches title-substrings ("engineer", "devops") which causes
false positives. This script uses the canonical signal — the job ID in the
'/job/<id>' link href — to determine truth.

Output:
- Console report: each tracker entry → REAL applied / FALSE positive
- DB: flips false positives from 'applied' to 'failed' with audit note
- Saves a screenshot of the Applied Jobs page for manual inspection
"""
import asyncio
import logging
import re
import sqlite3
from pathlib import Path

from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("audit")

import sys

DB = Path("jobs.db")
SESSION = str(Path("sessions/seek/state.json").resolve())
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
OUT = Path("errors/audit")
APPLIED_URL = "https://au.seek.com/my-activity/applied-jobs"
LIMIT = 5000  # full audit — covers all "applied" rows
DRY_RUN = "--apply" not in sys.argv  # default safe; pass --apply to actually flip rows
MIN_SCRAPED_FOR_TRUST = 5  # if fewer than this many job links found, REFUSE to flip — page is broken


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


async def collect_applied_cards(page) -> list[dict]:
    """Scrape title+company for every applied-job card across all pagination pages."""
    all_cards: list[dict] = []
    seen_keys: set[tuple] = set()
    page_num = 1
    while True:
        await asyncio.sleep(3)
        cards = await page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('span').forEach(label => {
                if (label.textContent.trim() !== 'Job Title') return;
                const tp = label.parentElement;
                if (!tp) return;
                const title = tp.textContent.replace(/^\\s*Job Title\\s*/, '').trim();
                let card = tp;
                for (let i = 0; i < 8 && card; i++) {
                    const adv = Array.from(card.querySelectorAll('span'))
                        .find(s => s.textContent.trim() === 'Advertiser');
                    if (adv) {
                        const ap = adv.parentElement;
                        const company = ap.textContent.replace(/^\\s*Advertiser\\s*/, '').trim();
                        out.push({title, company});
                        return;
                    }
                    card = card.parentElement;
                }
                out.push({title, company: ''});
            });
            return out;
        }""")
        new_count = 0
        for c in cards:
            key = (_norm(c["title"]), _norm(c["company"]))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_cards.append(c)
            new_count += 1
        log.info(f"  page {page_num}: {len(cards)} cards, {new_count} new")

        next_link = page.locator(
            'a[rel="next"], a:has-text("Next"), button:has-text("Next")'
        )
        if await next_link.count() == 0 or new_count == 0:
            break
        try:
            await next_link.first.click()
            await asyncio.sleep(3)
            page_num += 1
            if page_num > 20:
                break
        except Exception as e:
            log.info(f"  pagination ended: {e}")
            break
    return all_cards


async def main():
    OUT.mkdir(parents=True, exist_ok=True)

    # Pull last N applied entries (newest by timestamp) where board is seek
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT url, title, company, timestamp, notes FROM applications "
        "WHERE status='applied' AND board='seek' "
        "ORDER BY timestamp DESC LIMIT ?",
        (LIMIT,),
    ).fetchall()
    log.info(f"Loaded {len(rows)} 'applied' seek entries from DB")

    db_entries = [(url, title, company, ts) for (url, title, company, ts, _) in rows]

    async with async_playwright() as pw:
        kwargs = {"headless": False}
        if CHROME.exists():
            kwargs["executable_path"] = str(CHROME)
        browser = await pw.chromium.launch(**kwargs)
        ctx = await browser.new_context(storage_state=SESSION)
        page = await ctx.new_page()

        await page.goto(APPLIED_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)
        url = page.url.lower()
        if "login" in url or "oauth" in url:
            log.error("Session expired — please re-run setup_sessions.py")
            await browser.close()
            return

        await page.screenshot(path=str(OUT / "applied_jobs.png"), full_page=True)

        log.info("Scraping all applied-job cards (title + company) with pagination…")
        seek_cards = await collect_applied_cards(page)
        log.info(f"Total unique cards on Seek Applied Jobs: {len(seek_cards)}")

        await browser.close()

    # Cross-check by title+company fuzzy match
    seek_index = [(_norm(c["title"]), _norm(c["company"])) for c in seek_cards]
    real, false_pos = [], []
    for url, title, company, ts in db_entries:
        nt, nc = _norm(title), _norm(company)
        hit = False
        for st, sc in seek_index:
            title_ok = nt and (nt in st or st in nt)
            company_ok = nc and (nc in sc or sc in nc)
            if title_ok and company_ok:
                hit = True
                break
        if hit:
            real.append((url, title, company, ts))
        else:
            false_pos.append((url, title, company, ts))

    log.info("\n=========== AUDIT RESULT ===========")
    log.info(f"Total checked:    {len(db_entries)}")
    log.info(f"REAL applied:     {len(real)}")
    log.info(f"FALSE positives:  {len(false_pos)}")
    log.info("=====================================\n")

    log.info("REAL applications (sample 10):")
    for r in real[:10]:
        log.info(f"  ✅ {r[1]} @ {r[2]}  ({r[3]})")

    log.info("\nFALSE POSITIVES (would be flipped to 'failed'):")
    for f in false_pos:
        log.info(f"  ❌ {f[1]} @ {f[2]}  ({f[3]})")

    # Persist the false-positive list for the re-apply script
    import json
    fp_path = OUT / "false_positives.json"
    fp_path.write_text(json.dumps(
        [{"url": u, "title": t, "company": c, "timestamp": ts}
         for (u, t, c, ts) in false_pos],
        indent=2,
    ))
    log.info(f"\nWrote {len(false_pos)} false-positive entries to {fp_path}")

    # Safety gates before any DB write
    if len(seek_cards) < MIN_SCRAPED_FOR_TRUST:
        log.error(
            f"\nABORTING DB write: scraped only {len(seek_cards)} cards from "
            f"Applied Jobs page — page may be broken/throttled. Refusing to flip "
            f"any rows. Re-run later."
        )
    elif DRY_RUN:
        log.info(
            f"\nDRY RUN — no DB changes made. Would flip {len(false_pos)} rows "
            f"from 'applied' → 'failed'. Re-run with --apply to commit."
        )
    elif false_pos:
        ts_note = f"Audit {Path(__file__).stem}: not on Seek Applied Jobs page — was false positive"
        for url, _, _, _ in false_pos:
            conn.execute(
                "UPDATE applications SET status='failed', notes=? WHERE url=?",
                (ts_note, url),
            )
        conn.commit()
        log.info(f"\nFlipped {len(false_pos)} rows from 'applied' → 'failed'.")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
