"""EligibilityAnswerGuard: never select a citizenship / PR option on a work-rights
dropdown.

The observed bug was NOT in the LLM. On the AU_Q_6 right-to-work dropdown the engine
takes its rule-based path (seek_apply._answer_field): it builds the candidate's visa
label string and passes it to _select_best_option -> _safe_select_option, whose fuzzy
matcher picked "I'm an Australian citizen" over "I have a temporary visa that allows
me to work in Australia" for a non-citizen 485 holder. A false eligibility claim must
never reach a real submit.

Every dropdown selection (the rule path AND the LLM fallback) funnels through
seek_apply._safe_select_option, so that single function is the chokepoint. This wraps
it (no vendor edit, like ScreeningInterceptor): when the live DOM option set looks
like an eligibility question (it offers a citizen / PR / not-entitled option), the
guard forces the truthful safe option (a visa with work rights) and never a
disqualified one. If no safe option exists, or the dropdown is not an eligibility
question, the call delegates to the original untouched.
"""
from __future__ import annotations

import importlib
import logging
from contextlib import AbstractContextManager

logger = logging.getLogger(__name__)

# Options that are factually untrue for a non-citizen 485 holder, or that would
# misrepresent their status. Never pick these on a work-rights question. Their
# presence in a dropdown is also how we recognise an eligibility question.
_DISQUALIFY = (
    "australian citizen",
    "permanent resident",
    "australian pr",
    "new zealand citizen",
    "nz citizen",
    "require sponsor",
    "need sponsor",
    "sponsorship required",
    "will require sponsor",
    "would require sponsor",
    "student visa",
    "working holiday",
    "not currently entitled",
    "not entitled to work",
    "no right to work",
)

# Eligible options that positively state a visa with work rights (preferred pick).
_PREFER = (
    "485",
    "temporary graduate",
    "graduate visa",
    "no restriction",
    "no work restriction",
    "full work",
    "right to work",
    "allows me to work",
    "allowed to work",
    "any employer",
    "valid visa",
    "temporary visa",
    "work visa",
    "have a visa",
    "right to live and work",
    "post-study",
    "post study",
)

_ELIGIBILITY_KEYWORDS = (
    "right to work",
    "work in australia",
    "work rights",
    "working right",
    "citizen",
    "residency",
    "residen",
    "visa",
    "eligible to work",
    "entitled to work",
    "sponsor",
    "work authoris",
    "right to live and work",
    "work status",
)


def is_eligibility_question(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in _ELIGIBILITY_KEYWORDS)


def is_eligibility_option_set(option_texts) -> bool:
    """An eligibility dropdown is one that offers a disqualifying option (citizen /
    PR / not-entitled / sponsorship). That is exactly the dangerous case: the fuzzy
    matcher could land on it. Non-eligibility dropdowns (experience, salary, notice)
    contain none of these, so they delegate untouched."""
    return any(d in str(t).lower() for t in (option_texts or []) for d in _DISQUALIFY)


def safe_eligibility_pick(options) -> str | None:
    """The truthful option for a non-citizen 485 holder: never citizen/PR/etc.,
    preferring an explicit visa-with-work-rights option. None if every option is
    disqualified (the candidate genuinely cannot answer truthfully)."""
    opts = [str(o) for o in (options or [])]
    eligible = [o for o in opts if not any(d in o.lower() for d in _DISQUALIFY)]
    if not eligible:
        return None
    for o in eligible:
        if any(p in o.lower() for p in _PREFER):
            return o
    return eligible[0]


class EligibilityAnswerGuard(AbstractContextManager):
    def __init__(self, module_name: str = "seek_apply"):
        self._module_name = module_name
        self._mod = None
        self._original = None
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        try:
            mod = importlib.import_module(self._module_name)
        except Exception:
            return
        if not hasattr(mod, "_safe_select_option"):
            return
        original = mod._safe_select_option  # type: ignore[attr-defined]

        async def _patched(page, field_id, desired_value=None, desired_text=None):
            try:
                real = await mod._list_real_select_options(page, field_id)  # type: ignore[attr-defined]
            except Exception:
                real = None
            if real:
                texts = [str(o.get("text", "")) for o in real]
                if is_eligibility_option_set(texts):
                    pick = safe_eligibility_pick(texts)
                    if pick is not None:
                        chosen = next(
                            (o for o in real if str(o.get("text", "")) == pick), None
                        )
                        if chosen is not None:
                            logger.info(
                                "Eligibility guard: forcing %r (never claims "
                                "citizenship/PR) over caller hint value=%r text=%r",
                                pick,
                                desired_value,
                                desired_text,
                            )
                            return await original(
                                page,
                                field_id,
                                desired_value=chosen.get("value"),
                                desired_text=chosen.get("text"),
                            )
            return await original(
                page, field_id, desired_value=desired_value, desired_text=desired_text
            )

        self._mod = mod
        self._original = original
        mod._safe_select_option = _patched  # type: ignore[attr-defined]
        self._installed = True
        logger.info("EligibilityAnswerGuard installed on %s", self._module_name)

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            if self._mod is not None:
                self._mod._safe_select_option = self._original  # type: ignore[attr-defined]
        except Exception:
            pass
        self._installed = False

    def __enter__(self) -> "EligibilityAnswerGuard":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()
