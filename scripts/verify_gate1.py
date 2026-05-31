"""Gate-1 one-shot driver for the live verifier.

The pytest-based live verifier test
(`tests/integration/test_robust_verifier_live.py`) has a fixture that
creates the Playwright page in one `asyncio.run` and then calls
`verifier._verify(...)` in a different `asyncio.run`. Playwright Page
objects are bound to the event loop that created them; the cross-loop
call hangs forever on the underlying websocket.

This one-shot avoids the fixture problem entirely: it runs a single
`asyncio.run(main())` that opens the persistent context, runs the three
verifier scenarios in sequence, and prints structured results. The
verifier code itself is unchanged.

This script does NOT submit anything. It only navigates to
au.seek.com/my-activity/applied-jobs and reads cards. The persistent
context is closed cleanly at the end.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/verify_gate1.py

Pre-conditions identical to the pytest variant: no app instance holding
sessions/seek_chrome_profile/, valid Seek session, vendor/job-finder on
PYTHONPATH (this script adds it).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_ENGINE_WORKDIR = Path("/Users/sagarverma/Pictures/Claude-experiments/job-finder")
KNOWN_APPLIED_URL = "https://au.seek.com/job/92421026"
CONTROL_NOT_APPLIED_URL = "https://au.seek.com/job/92422709"

for p in (REPO_ROOT / "src", LIVE_ENGINE_WORKDIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _journal_set(url: str) -> None:
    import seek_apply  # type: ignore[import-not-found]
    seek_apply._Journal._data = {
        "url": url, "title": "", "company": "",
    }


def _journal_clear() -> None:
    import seek_apply  # type: ignore[import-not-found]
    seek_apply._Journal._data = None


async def main() -> int:
    # cwd matters: vendor/job-finder uses cwd-relative paths.
    prev_cwd = Path.cwd()
    os.chdir(LIVE_ENGINE_WORKDIR)
    try:
        import seek_apply  # type: ignore[import-not-found]
        from autoapply_next.engine.verifier import (
            RobustVerifier,
            VerifyOutcome,
        )

        sess = str(
            (LIVE_ENGINE_WORKDIR / "sessions" / "seek" / "state.json").resolve()
        )

        # Single asyncio loop: open page, run all three scenarios, close.
        print(f"[{ts()}] Gate-1: opening Playwright persistent context")
        page = await seek_apply._PeekSession.get_page(sess)

        rc = 0
        try:
            # 1. KNOWN APPLIED -- pass the REAL title and company the adapter
            # would read from jobs.db at apply time. This mirrors the
            # production call path: adapter._peek_and_fetch_listing reads
            # title/company from jobs.db, hands them to the engine, the
            # engine passes them into _verify_applied (our wrap). With this
            # input the title+company fuzzy fallback can match the card
            # the diagnostic confirmed is present on the page.
            print()
            print(f"[{ts()}] === Scenario 1: APPLIED on {KNOWN_APPLIED_URL} ===")
            _journal_set(KNOWN_APPLIED_URL)
            try:
                v = RobustVerifier(
                    poll_window_seconds=30, poll_interval_seconds=3.0
                )
                v.install()
                try:
                    result = await v._verify(
                        page,
                        "Automation Engineer",
                        "Hydrogen Group Pty Ltd",
                    )
                finally:
                    v.uninstall()
            finally:
                _journal_clear()
            ok1 = (
                result is True
                and v.last_state is not None
                and v.last_state.outcome == VerifyOutcome.APPLIED
            )
            print(
                f"[{ts()}] returned: {result!r}; "
                f"outcome={v.last_state.outcome.value if v.last_state else 'None'}; "
                f"strategy={v.last_state.matched_strategy if v.last_state else None}; "
                f"polls={v.last_state.polls_attempted if v.last_state else 0}; "
                f"cards_seen={v.last_state.cards_seen if v.last_state else 0}; "
                f"elapsed={v.last_state.elapsed_seconds:.1f}s"
            )
            print(f"[{ts()}] scenario 1: {'PASS' if ok1 else 'FAIL'}")
            if not ok1:
                rc = 2

            # 2. CONTROL NOT APPLIED
            print()
            print(
                f"[{ts()}] === Scenario 2: NOT_APPLIED on "
                f"{CONTROL_NOT_APPLIED_URL} ==="
            )
            _journal_set(CONTROL_NOT_APPLIED_URL)
            try:
                v = RobustVerifier(
                    poll_window_seconds=20, poll_interval_seconds=3.0
                )
                v.install()
                try:
                    result = await v._verify(
                        page,
                        "DefinitelyNotApplied-XXXXXXX",
                        "DefinitelyNotApplied-YYYYYYY",
                    )
                finally:
                    v.uninstall()
            finally:
                _journal_clear()
            ok2 = (
                result is False
                and v.last_state is not None
                and v.last_state.outcome == VerifyOutcome.NOT_APPLIED
                and v.last_state.cards_seen > 0
            )
            print(
                f"[{ts()}] returned: {result!r}; "
                f"outcome={v.last_state.outcome.value if v.last_state else 'None'}; "
                f"strategy={v.last_state.matched_strategy if v.last_state else None}; "
                f"polls={v.last_state.polls_attempted if v.last_state else 0}; "
                f"cards_seen={v.last_state.cards_seen if v.last_state else 0}; "
                f"elapsed={v.last_state.elapsed_seconds:.1f}s"
            )
            print(f"[{ts()}] scenario 2: {'PASS' if ok2 else 'FAIL'}")
            if not ok2:
                rc = 2

            # 3. UNREACHABLE -> UNCERTAIN (uses a synthetic page; no real navigation)
            print()
            print(f"[{ts()}] === Scenario 3: UNCERTAIN (synthetic page) ===")
            _journal_set(KNOWN_APPLIED_URL)
            bad_page = MagicMock()
            bad_page.goto = AsyncMock(side_effect=RuntimeError("offline"))
            bad_page.evaluate = AsyncMock(return_value=[])
            # The wrap calls seek_apply._scrape_applied_cards in the success
            # branch; stub it for this scenario.
            original_scrape = seek_apply._scrape_applied_cards

            async def stub_scrape(_page):
                return []

            seek_apply._scrape_applied_cards = stub_scrape
            try:
                v = RobustVerifier(
                    poll_window_seconds=2, poll_interval_seconds=0.1
                )
                v.install()
                try:
                    result = await v._verify(bad_page, "t", "c")
                finally:
                    v.uninstall()
            finally:
                seek_apply._scrape_applied_cards = original_scrape
                _journal_clear()
            ok3 = (
                result is True
                and v.last_state is not None
                and v.last_state.outcome == VerifyOutcome.UNCERTAIN
                and v.last_state.cards_seen == 0
            )
            print(
                f"[{ts()}] returned: {result!r}; "
                f"outcome={v.last_state.outcome.value if v.last_state else 'None'}; "
                f"polls={v.last_state.polls_attempted if v.last_state else 0}; "
                f"cards_seen={v.last_state.cards_seen if v.last_state else 0}; "
                f"elapsed={v.last_state.elapsed_seconds:.1f}s"
            )
            print(f"[{ts()}] scenario 3: {'PASS' if ok3 else 'FAIL'}")
            if not ok3:
                rc = 2

            print()
            if rc == 0:
                print(f"[{ts()}] GATE 1: GREEN LIGHT LIT (all 3 scenarios PASS)")
            else:
                print(f"[{ts()}] GATE 1: NOT LIT (at least one scenario FAILed)")
        finally:
            try:
                await page.close()
            except Exception:
                pass
            try:
                await seek_apply._PeekSession.close()
            except Exception:
                pass
        return rc
    finally:
        os.chdir(prev_cwd)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
