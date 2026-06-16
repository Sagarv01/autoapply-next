"""Client-side tailoring policy: Free/Basic apply with base docs, Pro tailors.

Mirrors the server's tiers.allows_tailoring (Pro only). The worker fetches the
user's tier once per batch and calls set_tailoring_allowed(allows_tailoring(tier));
the apply flow reads tailoring_allowed() to decide whether to LLM-tailor or use
the base resume + cover as-is. This is convenience/UX (don't waste an LLM call and
don't bounce a Free user off a 403 every job); the proxy's Pro-gate is still the
real enforcement.

Module-level (like llm_proxy's token provider) so it doesn't thread through
run_batch -> apply_to_job. Default True so any flow that never sets it keeps the
prior tailoring behavior.
"""
from __future__ import annotations


def allows_tailoring(tier) -> bool:
    """Per-job tailoring is Pro-only (legacy 'starter' = basic = no tailoring)."""
    return str(tier or "").strip().lower() == "pro"


_allowed = True


def set_tailoring_allowed(value) -> None:
    """Set whether the apply flow should LLM-tailor. None resets to the default."""
    global _allowed
    _allowed = True if value is None else bool(value)


def tailoring_allowed() -> bool:
    return _allowed
