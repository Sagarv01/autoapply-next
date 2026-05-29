"""Safety gate that neutralises the engine's final-submit step in dry-run mode.

# Why this exists

The vendored engine (`vendor/job-finder/seek_apply.py:_submit`) unconditionally clicks
the Seek "Submit application" button when the apply flow reaches the review screen.
Per ADR-0001 there is no in-engine dry-run flag, and per the project's hard rule we
do not edit the engine. The adapter therefore intercepts the single final-submit
action at runtime, in this module.

# The seam

`seek_apply._submit(page)` is the only function that performs the click. It is
called exactly once per application, from `apply_seek_quick` at
`seek_apply.py:256`. The call site uses a bare `_submit(...)` name, resolved at
call time from `seek_apply`'s module globals. Reassigning
`seek_apply._submit = <gated>` therefore takes effect for every subsequent call
without changing any source file on disk.

# Dry-run behaviour (allow_real_submit=False)

The gate's replacement function still drives the engine all the way to the
submit-ready state to prove readiness:

    1. Mirror the engine's behaviour and tick any "I agree" / consent checkbox,
       because the real submit button is sometimes disabled until the checkbox
       is ticked.
    2. Locate the submit button by the same selectors the engine uses.
    3. Assert it exists, is visible, and is enabled. If any check fails, raise
       `SeekApplyError` like the engine would. We deliberately do not raise
       DryRunReached here because "submit button missing" is a real failure that
       should surface to the user, not a successful dry-run.
    4. Take a full-page screenshot of the populated review screen.
    5. Raise `DryRunReached` so the engine flow stops without calling the real
       submit click and without proceeding to `_verify_applied` (which would
       just fail anyway because no application was actually filed).

# Live-submit behaviour (allow_real_submit=True)

The gate delegates to the captured original `_submit`. Behaviour is identical
to running the engine without the gate installed. The gate itself is still
installed in this mode because the user might toggle the flag at runtime
between jobs.

# Install / uninstall lifecycle

Use as a context manager:

    with SafetyGate(allow_real_submit=False, screenshot_dir=Path("...")) as gate:
        await applicator.apply(...)

The gate restores `seek_apply._submit` on exit even if an exception propagates.
Installation is idempotent: re-entering with the same instance is a no-op.

# Threading

The gate is intended to live in the engine worker thread alongside the asyncio
loop that drives the engine. Do not install / uninstall from the GUI thread.
The patch is process-wide (it mutates a module global), so do not run two
engines in the same process; the worker is a singleton.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DryRunReached(Exception):
    """Raised by the gated `_submit` when dry-run reaches submit-ready.

    Caught by `apply_to_job` and translated into a successful
    `ApplicationResult(status=DRY_RUN_VERIFIED)`.

    Not a `SeekApplyError`, deliberately, so the engine's existing try/except
    blocks do not treat it as a failure.
    """

    def __init__(self, *, screenshot_path: Path | None, submit_button_text: str):
        self.screenshot_path = screenshot_path
        self.submit_button_text = submit_button_text
        loc = str(screenshot_path) if screenshot_path is not None else "(no screenshot)"
        super().__init__(
            f"DRY-RUN: reached submit-ready; would have clicked "
            f"'{submit_button_text}'. Screenshot: {loc}"
        )


@dataclass
class _GateState:
    """Per-install state, kept off the class so a missed `uninstall` is loud."""

    allow_real_submit: bool
    screenshot_dir: Path
    original_submit: Any
    """The captured original `seek_apply._submit` function."""


class SafetyGate(AbstractContextManager):
    """Runtime gate around `seek_apply._submit`.

    Use as a context manager. The gate must be installed before any call to
    `applicator.apply` / `apply_seek_quick`, and must be uninstalled afterwards.

    Parameters
    ----------
    allow_real_submit:
        If False (the default and the only safe value during testing), dry-run
        behaviour as documented in the module docstring. If True, the gate
        delegates to the original `_submit` and a real Seek application is
        filed. **The test harness must never set this to True.**
    screenshot_dir:
        Where to write the dry-run screenshot. Created if missing. Must be a
        writable directory. Per-call screenshot filenames are timestamped.
    """

    def __init__(self, *, allow_real_submit: bool, screenshot_dir: Path):
        self._allow_real_submit = bool(allow_real_submit)
        self._screenshot_dir = Path(screenshot_dir)
        self._state: _GateState | None = None

    # ------------------------------------------------------------------ public

    @property
    def allow_real_submit(self) -> bool:
        return self._allow_real_submit

    @property
    def installed(self) -> bool:
        return self._state is not None

    def install(self) -> None:
        if self._state is not None:
            return  # idempotent
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        # Importing seek_apply triggers the engine's module-load side effects.
        # The caller must have set up cwd / env first. The adapter does this.
        import seek_apply  # type: ignore[import-not-found]

        original = seek_apply._submit
        gate = self  # closure capture

        async def gated_submit(page):
            if gate._allow_real_submit:
                logger.warning(
                    "SafetyGate: allow_real_submit=True; calling original _submit "
                    "(this will file a real Seek application)"
                )
                return await original(page)
            await _dry_run_submit(page, gate._screenshot_dir)

        seek_apply._submit = gated_submit  # type: ignore[attr-defined]
        self._state = _GateState(
            allow_real_submit=self._allow_real_submit,
            screenshot_dir=self._screenshot_dir,
            original_submit=original,
        )
        logger.info(
            "SafetyGate installed (allow_real_submit=%s, screenshot_dir=%s)",
            self._allow_real_submit,
            self._screenshot_dir,
        )

    def uninstall(self) -> None:
        if self._state is None:
            return
        import seek_apply  # type: ignore[import-not-found]

        seek_apply._submit = self._state.original_submit  # type: ignore[attr-defined]
        self._state = None
        logger.info("SafetyGate uninstalled (original _submit restored)")

    # ---------------------------------------------------------- context mgr

    def __enter__(self) -> "SafetyGate":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()


# --------------------------------------------------------------------- helpers


# Submit button selector list, kept in sync with vendor/job-finder/seek_apply.py
# version c85c68d2. If the engine adds a new selector, mirror it here. Drift
# is caught by the contract test `test_submit_selectors_match_engine`.
_SUBMIT_SELECTORS = ["Submit application", "Submit", "Apply now", "Apply"]


async def _dry_run_submit(page, screenshot_dir: Path) -> None:
    """The gated replacement for seek_apply._submit when allow_real_submit=False.

    Drives the engine's pre-submit hygiene (consent checkbox), proves the submit
    button is ready, captures a screenshot, then raises DryRunReached.
    """
    import asyncio

    import seek_apply  # type: ignore[import-not-found]

    # 1. Match engine behaviour for consent checkbox. If this fails for any
    #    reason, the engine just warns and continues, so we do the same.
    await asyncio.sleep(1)
    try:
        await seek_apply._tick_terms_checkbox(page)
    except Exception as exc:
        logger.warning(
            "SafetyGate dry-run: _tick_terms_checkbox raised %s; continuing",
            exc,
        )

    # 2/3. Locate, scroll, and assert state on each candidate selector. Use the
    #      first one that matches, like the engine does.
    submit_button_text = None
    for selector_text in _SUBMIT_SELECTORS:
        btn = page.get_by_role("button", name=selector_text, exact=False)
        if await btn.count() == 0:
            continue
        first = btn.first
        try:
            await first.scroll_into_view_if_needed()
        except Exception:
            # Engine ignores this in the success path; we do too. We still
            # require visible+enabled below.
            pass
        if not await first.is_visible():
            continue
        if not await first.is_enabled():
            continue
        submit_button_text = selector_text
        break

    if submit_button_text is None:
        # Not a successful dry-run. Raise the engine's own error type so the
        # outer flow treats it as a failure to surface to the user.
        raise seek_apply.SeekApplyError(
            "DRY-RUN gate: reached the review step but no submit button was "
            "present, visible, and enabled. The engine would also have failed "
            "to submit. Selectors tried: "
            + ", ".join(repr(s) for s in _SUBMIT_SELECTORS)
        )

    # 4. Screenshot the populated review screen.
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    screenshot_path = screenshot_dir / f"dryrun-submit-ready-{ts}.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception as exc:
        # Screenshot failure is not safety-critical, but tell the user.
        logger.warning(
            "SafetyGate dry-run: screenshot failed (%s). Continuing.", exc
        )
        screenshot_path = None  # surface "missing" in DryRunReached

    logger.info(
        "SafetyGate dry-run: submit-ready VERIFIED on selector %r. "
        "Screenshot=%s. Not submitting.",
        submit_button_text,
        screenshot_path,
    )

    # 5. Hand control back to the adapter via a sentinel exception.
    raise DryRunReached(
        screenshot_path=screenshot_path,
        submit_button_text=submit_button_text,
    )
