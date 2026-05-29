"""Directly invoke seek_apply.peek_is_quick_apply on a batch of URLs using
the bot's exact code path (_PeekSession persistent context). Compare against
diagnose_peek.py which uses a fresh context per URL."""
import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

from seek_apply import fetch_seek_jd, peek_is_quick_apply, _PeekSession

URLS = [
    ("https://au.seek.com/job/91860061", "Service Delivery Manager — manual diag said quick=YES"),
    ("https://au.seek.com/job/91870805", "Senior Tech & Sec Consultant — manual diag said external"),
    ("https://au.seek.com/job/91871000", "AI Engineer — manual diag said external"),
    ("https://au.seek.com/job/91872326", "Expressions of Interest — manual diag said quick=YES"),
]


async def main():
    session_state = str(Path("sessions/seek/state.json").resolve())
    for url, note in URLS:
        print(f"\n=== {url} ===")
        print(f"  {note}")
        is_quick, page = await peek_is_quick_apply(url, session_state)
        jd = await fetch_seek_jd(url, session_state) if is_quick else ""
        print(f"  >>> peek_is_quick_apply returned: is_quick={is_quick}, jd_len={len(jd)}")
        if page:
            try:
                print(f"      page.url={page.url}")
                await page.close()
            except Exception:
                pass
    await _PeekSession.close()


if __name__ == "__main__":
    asyncio.run(main())
