"""Runtime wrap of seek_apply._verify_applied: robust + three-state.

# Why this exists

The vendored engine's `_verify_applied` (vendor/job-finder/seek_apply.py:2119)
checks whether a freshly-submitted application is on the Applied Jobs page
by scraping the cards and doing a fuzzy substring match on
`title` AND `company`. Two failure modes are visible in the wild:

1. **False negative**: the engine treats "could not verify" as "failed", so
   the caller raises `SeekApplyError("Submission not verified ... Marking
   as failed for manual retry.")`. The submission may have actually
   succeeded; the engine just could not see it yet (Seek lag, missing
   metadata in the card scrape, etc.). A daemon that auto-retried on
   "failed" would double-submit. That is unacceptable.

2. **Bad target metadata**: if the caller passes a placeholder
   `job_title` and `job_company=""` (which is what our adapter did for
   GUI-submitted jobs before this fix), title+company can never match.
   The engine cannot tell the difference between "could not find" and
   "was never going to find."

# Three outcomes

This wrap monkey-patches `seek_apply._verify_applied` and reports one of
three states via the `last_state` accessor:

- `APPLIED`      - matched on the Applied Jobs page (preferred: by Seek
                   job id in any href on the page; fallback: by fuzzy
                   normalized title+company). Returns `True`; the engine
                   reports 'applied'.
- `NOT_APPLIED`  - confirmed absent after polling the full window
                   (cards were readable, the job's id and metadata are
                   not present). Returns `False`; the engine raises
                   `SeekApplyError` and our adapter surfaces FAILED.
- `UNCERTAIN`    - could not read the page (all polls errored, missing
                   journal data, etc.). Returns `True` so the engine
                   reports 'applied' and **does not raise**. The adapter
                   reads `last_state.outcome` and marks the result
                   `SUBMITTED_UNCERTAIN`. This is the no-auto-retry
                   guarantee: an inconclusive verify never becomes a
                   "failed" row that a future loop might re-submit.

# Why match by job id first

Seek puts the job id in the href of cards on the Applied Jobs page
(`<a href="https://au.seek.com/job/12345?...">`). Card text varies
(truncation, "via Seek" suffixes, recruiter aliases like "Hydrogen Group
Pty Ltd" vs "Hydrogen Group"), but the job id is the canonical key. We
match on it first; title+company is the fallback for the rare case where
the page does not render an href with the id.

# Engine source untouched

We import `seek_apply`, capture the original `_verify_applied`, and
replace the module attribute. Same pattern as `safety.SafetyGate._submit`.
No file under `vendor/` is touched.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class VerifyOutcome(str, Enum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNCERTAIN = "uncertain"


@dataclass
class VerificationState:
    """The verifier writes this on every call. Adapter reads it after
    `applicator.apply()` returns to decide SUBMITTED vs SUBMITTED_UNCERTAIN."""

    outcome: VerifyOutcome
    matched_job_id: str | None = None
    matched_strategy: str | None = None
    """'job_id' | 'title_company' | None"""
    cards_seen: int = 0
    """Maximum number of cards we managed to read in any single poll."""
    polls_attempted: int = 0
    elapsed_seconds: float = 0.0
    detail: str = ""


# ----------------------------------------------------------------------- helpers


def extract_seek_job_id(url: str) -> str | None:
    """Pull '12345' from 'https://au.seek.com/job/12345...' / .../apply / ?type=."""
    if not url:
        return None
    m = re.search(r"/job/(\d+)", url)
    return m.group(1) if m else None


_VIA_SUFFIXES = ("via seek.com.au", "via seek.com", "via seek")
_TRIM_CHARS = " -|,–—"  # space, hyphen, pipe, comma, en-dash, em-dash


def normalize_text(s: str) -> str:
    """Lowercase, collapse whitespace, strip trailing 'via Seek' variants
    and any trailing ellipsis (literal '...' or the Unicode '…' character)."""
    if not s:
        return ""
    s = " ".join(s.lower().split())
    # Strip trailing ellipsis first so the via-seek strip below also lands
    # cleanly. Repeated strips handle 'Foo … via Seek'.
    for _ in range(3):
        changed = False
        for suf in _VIA_SUFFIXES:
            if s.endswith(suf):
                s = s[: -len(suf)].rstrip(_TRIM_CHARS)
                changed = True
        for ell in ("...", "…"):
            if s.endswith(ell):
                s = s[: -len(ell)].rstrip(_TRIM_CHARS)
                changed = True
        if not changed:
            break
    return s


def title_company_match(
    card_title: str,
    card_company: str,
    target_title: str,
    target_company: str,
) -> bool:
    """Fuzzy substring match on normalized values; both axes required.

    Truncation-tolerant: if the card text is truncated, substring matches
    either way (`card in target` OR `target in card`). Empty targets fail.
    """
    ct = normalize_text(card_title)
    cc = normalize_text(card_company)
    tt = normalize_text(target_title)
    tc = normalize_text(target_company)
    if not (tt and tc):
        return False
    title_ok = ct in tt or tt in ct
    company_ok = cc in tc or tc in cc
    return title_ok and company_ok


def find_job_id_in_html(html: str, job_id: str) -> bool:
    """Scan rendered HTML for a Seek /job/<id> URL fragment. Used by both
    the live verifier (via page.evaluate) and the unit tests (which pass
    captured HTML directly)."""
    if not job_id or not html:
        return False
    return bool(re.search(rf"/job/{re.escape(job_id)}(?:[/?#\"'\s]|$)", html))


# JS evaluated inside the live page. Returns the list of unique job ids
# discovered in any `href` attribute on the rendered page.
_FIND_JOB_IDS_JS = r"""
() => {
    const ids = new Set();
    document.querySelectorAll('a[href]').forEach(a => {
        const m = a.href.match(/\/job\/(\d+)/);
        if (m) ids.add(m[1]);
    });
    return Array.from(ids);
}
"""


# --------------------------------------------------------------------- wrapper


class RobustVerifier(AbstractContextManager):
    """Monkey-patches `seek_apply._verify_applied` for the duration of one
    apply. Context manager; install/uninstall idempotent."""

    def __init__(
        self,
        *,
        poll_window_seconds: int = 30,
        poll_interval_seconds: float = 3.0,
    ):
        self._poll_window = max(1, int(poll_window_seconds))
        self._poll_interval = max(0.5, float(poll_interval_seconds))
        self._original: Any = None
        self._installed = False
        self._last_state: VerificationState | None = None

    @property
    def last_state(self) -> VerificationState | None:
        return self._last_state

    @property
    def installed(self) -> bool:
        return self._installed

    def install(self) -> None:
        if self._installed:
            return
        import seek_apply  # type: ignore[import-not-found]

        self._original = seek_apply._verify_applied
        verifier = self  # closure capture

        async def wrapped(page, job_title, job_company):
            return await verifier._verify(page, job_title, job_company)

        seek_apply._verify_applied = wrapped  # type: ignore[attr-defined]
        self._installed = True
        logger.info(
            "RobustVerifier installed (poll_window=%ds, interval=%.1fs)",
            self._poll_window,
            self._poll_interval,
        )

    def uninstall(self) -> None:
        if not self._installed:
            return
        import seek_apply  # type: ignore[import-not-found]

        seek_apply._verify_applied = self._original  # type: ignore[attr-defined]
        self._installed = False
        logger.info("RobustVerifier uninstalled")

    def __enter__(self) -> "RobustVerifier":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()

    # ------------------------------------------------------------ verify

    async def _verify(self, page, job_title: str, job_company: str) -> bool:
        import seek_apply  # type: ignore[import-not-found]

        journal_data = getattr(seek_apply._Journal, "_data", None) or {}
        url = journal_data.get("url", "")
        job_id = extract_seek_job_id(url)

        started = time.monotonic()
        deadline = started + self._poll_window
        polls_attempted = 0
        max_cards_seen = 0
        last_error: str | None = None

        while time.monotonic() < deadline:
            polls_attempted += 1
            try:
                await page.goto(
                    "https://au.seek.com/my-activity/applied-jobs",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                # Gentle backoff: later polls wait longer for hydration.
                await asyncio.sleep(min(5.0, 1.5 + 0.7 * polls_attempted))
            except Exception as exc:
                last_error = f"goto: {type(exc).__name__}: {exc}"
                logger.warning(
                    "RobustVerifier poll %d goto errored: %s",
                    polls_attempted,
                    last_error,
                )
                await asyncio.sleep(self._poll_interval)
                continue

            # Strategy 1: job-id match (most robust).
            if job_id:
                try:
                    ids = await page.evaluate(_FIND_JOB_IDS_JS)
                    if job_id in (ids or []):
                        self._last_state = VerificationState(
                            outcome=VerifyOutcome.APPLIED,
                            matched_job_id=job_id,
                            matched_strategy="job_id",
                            cards_seen=max_cards_seen,
                            polls_attempted=polls_attempted,
                            elapsed_seconds=time.monotonic() - started,
                            detail=(
                                f"matched job id {job_id} in href on poll "
                                f"{polls_attempted}"
                            ),
                        )
                        logger.info(
                            "RobustVerifier APPLIED via job_id=%s on poll %d",
                            job_id,
                            polls_attempted,
                        )
                        return True
                except Exception as exc:
                    last_error = f"job_id eval: {type(exc).__name__}: {exc}"
                    logger.warning(
                        "RobustVerifier job-id JS errored on poll %d: %s",
                        polls_attempted,
                        last_error,
                    )

            # Strategy 2: title+company fuzzy match.
            try:
                cards = await seek_apply._scrape_applied_cards(page)
            except Exception as exc:
                cards = []
                last_error = f"scrape: {type(exc).__name__}: {exc}"
                logger.warning(
                    "RobustVerifier card scrape errored on poll %d: %s",
                    polls_attempted,
                    last_error,
                )
            max_cards_seen = max(max_cards_seen, len(cards))
            for c in cards:
                if title_company_match(
                    c.get("title", ""),
                    c.get("company", ""),
                    job_title,
                    job_company,
                ):
                    self._last_state = VerificationState(
                        outcome=VerifyOutcome.APPLIED,
                        matched_job_id=job_id,
                        matched_strategy="title_company",
                        cards_seen=max_cards_seen,
                        polls_attempted=polls_attempted,
                        elapsed_seconds=time.monotonic() - started,
                        detail=(
                            "matched title+company on poll "
                            f"{polls_attempted}"
                        ),
                    )
                    logger.info(
                        "RobustVerifier APPLIED via title+company on poll %d",
                        polls_attempted,
                    )
                    return True

            logger.info(
                "RobustVerifier poll %d: %d cards, job_id=%s not seen yet",
                polls_attempted,
                len(cards),
                job_id,
            )
            await asyncio.sleep(self._poll_interval)

        elapsed = time.monotonic() - started

        if max_cards_seen > 0:
            self._last_state = VerificationState(
                outcome=VerifyOutcome.NOT_APPLIED,
                matched_job_id=job_id,
                cards_seen=max_cards_seen,
                polls_attempted=polls_attempted,
                elapsed_seconds=elapsed,
                detail=(
                    f"not present after polling {self._poll_window}s "
                    f"({polls_attempted} polls, {max_cards_seen} cards seen)"
                ),
            )
            logger.info(
                "RobustVerifier NOT_APPLIED (%.1fs, %d polls, %d cards)",
                elapsed,
                polls_attempted,
                max_cards_seen,
            )
            return False

        # No poll managed to read the page. Don't fail; surface UNCERTAIN.
        self._last_state = VerificationState(
            outcome=VerifyOutcome.UNCERTAIN,
            matched_job_id=job_id,
            cards_seen=0,
            polls_attempted=polls_attempted,
            elapsed_seconds=elapsed,
            detail=(
                f"page errors throughout {polls_attempted} polls; "
                f"last_error={last_error or '(none)'}"
            ),
        )
        logger.warning(
            "RobustVerifier UNCERTAIN (%.1fs, %d polls, last_error=%s)",
            elapsed,
            polls_attempted,
            last_error,
        )
        # Return True so the engine reports 'applied' rather than raising
        # SeekApplyError. The adapter must inspect last_state and surface
        # SUBMITTED_UNCERTAIN to the user.
        return True
