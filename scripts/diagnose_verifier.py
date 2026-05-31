"""One-shot diagnostic: dump what the verifier actually sees on Seek's
applied-jobs page. Read-only; navigates only, no submission."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE = Path("/Users/sagarverma/Pictures/Claude-experiments/job-finder")
for p in (REPO_ROOT / "src", LIVE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


async def main() -> int:
    os.chdir(LIVE)
    import seek_apply  # type: ignore[import-not-found]

    sess = str((LIVE / "sessions" / "seek" / "state.json").resolve())
    print("opening persistent context...")
    page = await seek_apply._PeekSession.get_page(sess)
    try:
        print("navigating to applied-jobs...")
        await page.goto(
            "https://au.seek.com/my-activity/applied-jobs",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(5)  # extra hydration
        print(f"final url: {page.url}")
        print(f"title:     {await page.title()}")
        html = await page.content()
        Path("/tmp/applied-jobs-diagnose.html").write_text(html, encoding="utf-8")
        print(f"html bytes: {len(html)}")

        # All /job/<id> ids found anywhere in href attrs.
        ids = sorted(set(re.findall(r"/job/(\d+)", html)))
        print(f"unique /job/<id> ids in href: {len(ids)}")
        print(f"first 30 ids: {ids[:30]}")
        print(f"92421026 in href? {'92421026' in ids}")
        print(f"92421026 anywhere in html? {'92421026' in html}")

        # Engine's card scrape result.
        cards = await seek_apply._scrape_applied_cards(page)
        print(f"engine cards scraped: {len(cards)}")
        print(f"first 5 card titles: {[c.get('title', '')[:60] for c in cards[:5]]}")
        # Is the Automation Engineer card present?
        ae = [
            c for c in cards
            if "automation" in (c.get("title") or "").lower()
        ]
        print(f"cards mentioning 'automation': {len(ae)}")
        for c in ae[:3]:
            print(f"  - title={c.get('title')!r}  company={c.get('company')!r}")

        await page.screenshot(path="/tmp/applied-jobs-diagnose.png", full_page=True)
        print("screenshot: /tmp/applied-jobs-diagnose.png")
    finally:
        try:
            await page.close()
        except Exception:
            pass
        try:
            await seek_apply._PeekSession.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
