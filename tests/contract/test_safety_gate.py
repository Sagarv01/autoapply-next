"""Contract tests for the safety gate.

These tests verify the gate's promises *without* hitting Seek. They use a
synthetic in-process `seek_apply` module so the gate's monkey-patch site
behaves the same as the real engine's.

The critical assertions:

1. With `allow_real_submit=False`, the gated `_submit` never calls the
   original `_submit`.
2. The gated `_submit` raises `DryRunReached` when the page reports a
   visible, enabled submit button.
3. The gated `_submit` raises `SeekApplyError` when no submit button is
   ready (engine-equivalent failure).
4. With `allow_real_submit=True`, the gated `_submit` does call the
   original.
5. The gate is reversible: after `uninstall`, `seek_apply._submit` is the
   original.
6. The gate is idempotent on install.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _build_fake_seek_apply(tmp_path: Path) -> tuple[types.ModuleType, dict]:
    """Inject a minimal `seek_apply` stub into sys.modules and return a
    side-channel dict the test can use to inspect what was called.

    The stub provides exactly what the gate needs:
      - `_submit(page)` async function (the original, will be patched)
      - `SeekApplyError`
      - `_tick_terms_checkbox(page)` async function
    """
    sentinel = {
        "original_submit_calls": 0,
        "ticked_calls": 0,
    }

    class SeekApplyError(Exception):
        pass

    async def original_submit(page):
        sentinel["original_submit_calls"] += 1

    async def tick_terms(page):
        sentinel["ticked_calls"] += 1

    module = types.ModuleType("seek_apply")
    module._submit = original_submit
    module.SeekApplyError = SeekApplyError
    module._tick_terms_checkbox = tick_terms
    sys.modules["seek_apply"] = module
    return module, sentinel


def _make_page_with_ready_submit() -> MagicMock:
    """Build a stub Playwright Page where the 'Submit application' button is
    present, visible, and enabled. Mirrors the real Page API shape that the
    gate calls into."""
    page = MagicMock(name="Page")

    button = MagicMock(name="Locator")
    button.count = AsyncMock(return_value=1)
    first = MagicMock(name="LocatorFirst")
    first.scroll_into_view_if_needed = AsyncMock(return_value=None)
    first.is_visible = AsyncMock(return_value=True)
    first.is_enabled = AsyncMock(return_value=True)
    button.first = first

    def get_by_role(role, *, name, exact=False):
        # Return the ready button for the first selector "Submit application".
        if name == "Submit application":
            return button
        # All other selectors report count=0 so the loop short-circuits on
        # the first one (matching how the engine prefers "Submit application").
        empty = MagicMock(name=f"Locator(name={name})")
        empty.count = AsyncMock(return_value=0)
        return empty

    page.get_by_role = MagicMock(side_effect=get_by_role)
    page.screenshot = AsyncMock(return_value=None)
    return page


def _make_page_with_no_submit() -> MagicMock:
    """Page where no submit button is found under any selector."""
    page = MagicMock(name="Page")

    def get_by_role(role, *, name, exact=False):
        empty = MagicMock(name=f"Locator(name={name})")
        empty.count = AsyncMock(return_value=0)
        return empty

    page.get_by_role = MagicMock(side_effect=get_by_role)
    page.screenshot = AsyncMock(return_value=None)
    return page


def _make_page_with_disabled_submit() -> MagicMock:
    """Page where submit button exists but is disabled (required field missed)."""
    page = MagicMock(name="Page")

    def make_button_disabled():
        button = MagicMock(name="Locator")
        button.count = AsyncMock(return_value=1)
        first = MagicMock(name="LocatorFirst")
        first.scroll_into_view_if_needed = AsyncMock(return_value=None)
        first.is_visible = AsyncMock(return_value=True)
        first.is_enabled = AsyncMock(return_value=False)
        button.first = first
        return button

    def get_by_role(role, *, name, exact=False):
        return make_button_disabled()

    page.get_by_role = MagicMock(side_effect=get_by_role)
    page.screenshot = AsyncMock(return_value=None)
    return page


# ----------------------------------------------------------------------------- tests


def test_install_replaces_submit_and_uninstall_restores(tmp_path):
    """Symmetry: after install, _submit is the gated version; after uninstall,
    _submit is the original. Idempotent."""
    from autoapply_next.engine.safety import SafetyGate

    module, _ = _build_fake_seek_apply(tmp_path)
    original = module._submit

    gate = SafetyGate(
        allow_real_submit=False, screenshot_dir=tmp_path / "shots"
    )
    assert not gate.installed

    gate.install()
    assert gate.installed
    assert module._submit is not original

    # Idempotent.
    gate.install()
    assert gate.installed

    gate.uninstall()
    assert not gate.installed
    assert module._submit is original

    # Idempotent uninstall.
    gate.uninstall()
    assert not gate.installed


def test_context_manager_restores_on_exception(tmp_path):
    from autoapply_next.engine.safety import SafetyGate

    module, _ = _build_fake_seek_apply(tmp_path)
    original = module._submit

    with pytest.raises(RuntimeError):
        with SafetyGate(
            allow_real_submit=False, screenshot_dir=tmp_path / "shots"
        ):
            assert module._submit is not original
            raise RuntimeError("boom")

    assert module._submit is original


def test_dry_run_never_calls_original_submit(tmp_path):
    """The headline safety claim. With allow_real_submit=False, the original
    `_submit` is NEVER called, even when the page reports a ready submit
    button. The gate raises DryRunReached instead."""
    from autoapply_next.engine.safety import DryRunReached, SafetyGate

    module, sentinel = _build_fake_seek_apply(tmp_path)
    page = _make_page_with_ready_submit()

    with SafetyGate(
        allow_real_submit=False, screenshot_dir=tmp_path / "shots"
    ):
        with pytest.raises(DryRunReached) as exc_info:
            asyncio.run(module._submit(page))

    assert sentinel["original_submit_calls"] == 0
    assert sentinel["ticked_calls"] == 1
    assert exc_info.value.submit_button_text == "Submit application"
    assert exc_info.value.screenshot_path.parent == (tmp_path / "shots").resolve() or \
        exc_info.value.screenshot_path.parent == tmp_path / "shots"
    page.screenshot.assert_awaited()


def test_dry_run_with_no_submit_button_raises_seek_apply_error(tmp_path):
    """If submit button is missing at review screen, the gate raises the
    engine's own error type so the outer flow treats it as a real failure
    (the engine would also have failed)."""
    from autoapply_next.engine.safety import SafetyGate

    module, sentinel = _build_fake_seek_apply(tmp_path)
    page = _make_page_with_no_submit()

    with SafetyGate(
        allow_real_submit=False, screenshot_dir=tmp_path / "shots"
    ):
        with pytest.raises(module.SeekApplyError):
            asyncio.run(module._submit(page))

    assert sentinel["original_submit_calls"] == 0


def test_dry_run_with_disabled_submit_raises_seek_apply_error(tmp_path):
    """Disabled button means a required field is unanswered. Treat as failure;
    do not consider this a successful dry-run."""
    from autoapply_next.engine.safety import SafetyGate

    module, sentinel = _build_fake_seek_apply(tmp_path)
    page = _make_page_with_disabled_submit()

    with SafetyGate(
        allow_real_submit=False, screenshot_dir=tmp_path / "shots"
    ):
        with pytest.raises(module.SeekApplyError):
            asyncio.run(module._submit(page))

    assert sentinel["original_submit_calls"] == 0


def test_real_submit_calls_original(tmp_path):
    """The mirror test. With allow_real_submit=True, the gated wrapper
    delegates to the original. This proves the gate is a true wrapper and not
    an unconditional block."""
    from autoapply_next.engine.safety import SafetyGate

    module, sentinel = _build_fake_seek_apply(tmp_path)
    page = _make_page_with_ready_submit()

    with SafetyGate(
        allow_real_submit=True, screenshot_dir=tmp_path / "shots"
    ):
        asyncio.run(module._submit(page))

    assert sentinel["original_submit_calls"] == 1


def test_dry_run_tick_failure_does_not_block_gate(tmp_path):
    """If `_tick_terms_checkbox` raises (e.g. no checkbox on this form), the
    gate logs and continues, matching the engine's behaviour. The dry-run
    still succeeds."""
    from autoapply_next.engine.safety import DryRunReached, SafetyGate

    module, sentinel = _build_fake_seek_apply(tmp_path)

    async def bad_tick(page):
        sentinel["ticked_calls"] += 1
        raise RuntimeError("no checkbox")

    module._tick_terms_checkbox = bad_tick

    page = _make_page_with_ready_submit()
    with SafetyGate(
        allow_real_submit=False, screenshot_dir=tmp_path / "shots"
    ):
        with pytest.raises(DryRunReached):
            asyncio.run(module._submit(page))

    assert sentinel["ticked_calls"] == 1
    assert sentinel["original_submit_calls"] == 0


def test_screenshot_failure_does_not_block_gate(tmp_path):
    """If page.screenshot raises (e.g. headless browser closed), the gate
    still raises DryRunReached so the adapter knows submit-ready was
    achieved. The user gets a missing-screenshot signal in the result."""
    from autoapply_next.engine.safety import DryRunReached, SafetyGate

    module, sentinel = _build_fake_seek_apply(tmp_path)
    page = _make_page_with_ready_submit()
    page.screenshot.side_effect = RuntimeError("page closed")

    with SafetyGate(
        allow_real_submit=False, screenshot_dir=tmp_path / "shots"
    ):
        with pytest.raises(DryRunReached) as exc_info:
            asyncio.run(module._submit(page))

    assert sentinel["original_submit_calls"] == 0
    assert exc_info.value.screenshot_path is None


def test_gate_selectors_match_engine(engine_workdir):
    """Drift test: assert the gate's submit selector list is a subset of the
    engine's. If the engine adds a selector we don't know about, the gate's
    dry-run might miss the ready button and report a false negative; fix is
    to mirror the new selector in `safety._SUBMIT_SELECTORS`."""
    import seek_apply  # vendor

    from autoapply_next.engine import safety

    # Engine declares submit selectors inline in `_submit`. We grep the
    # source instead of importing because there is no symbol to capture.
    import inspect

    src = inspect.getsource(seek_apply._submit)
    for selector in safety._SUBMIT_SELECTORS:
        assert (
            f'"{selector}"' in src
        ), (
            f"Gate selector {selector!r} not found in engine's _submit. "
            "Update safety._SUBMIT_SELECTORS to match the engine."
        )
