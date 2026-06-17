"""EligibilityAnswerGuard: answer a Seek work-rights dropdown truthfully for the
candidate's actual visa, or hold it for the user. Never claim a status or visa type
they do not hold.

Two false-claim bugs were observed live on the AU_Q_6 right-to-work dropdown, both
from the engine's RULE path (seek_apply._answer_field builds the candidate's visa
label string and fuzzy-matches it against the options), never the LLM:
  1. the fuzzy matcher picked "I'm an Australian citizen"; and
  2. it picked "I have a family/partner visa with no restrictions" (it matched
     "no restrictions") for a 485 Temporary Graduate holder.
Both are false. A false eligibility claim must never reach a real submit.

This wraps seek_apply._answer_field (no vendor edit, like ScreeningInterceptor). For
an eligibility dropdown it selects the option consistent with the candidate's actual
visa (485 / a temporary visa with work rights / an explicit "Other"), excluding any
citizen/PR/not-entitled option AND any option naming a different visa type
(family/partner/student/working-holiday/...). When no truthful option exists it
raises QuestionHeldError so the user answers it once (remembered thereafter), rather
than guessing. Non-eligibility dropdowns and non-selects delegate untouched.
"""
from __future__ import annotations

import importlib
import logging
from contextlib import AbstractContextManager

from ..screening.interceptor import QuestionHeldError, job_id_from_listing

logger = logging.getLogger(__name__)

# Factually false for a non-citizen 485 holder, or misleading. Never pick these; and
# their presence in a dropdown is how we recognise an eligibility question.
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
    "not currently entitled",
    "not entitled to work",
    "no right to work",
    "no work rights",
)

# Specific visa types the candidate does NOT hold. Picking one is a false claim even
# if it says "no restrictions". Excluded unless the term is in the candidate's own
# visa label (so a real "graduate"/"temporary" match is never dropped).
_WRONG_VISA_TYPES = (
    "family",
    "partner",
    "spouse",
    "de facto",
    "parent",
    "student",
    "working holiday",
    "work and holiday",
    "bridging",
    "business",
    "investor",
    "retirement",
    "refugee",
    "humanitarian",
    "skilled regional",
)

# A temporary visa WITH work restrictions is false for a full-work-rights 485.
_RESTRICTED = (
    "work restriction",
    "has restriction",
    "with restriction",
    "restrictions apply",
    "limited hour",
    "work limitation",
    "condition 8105",
    "8105",
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
    """An eligibility dropdown offers a disqualifying option (citizen / PR /
    not-entitled). That is exactly the dangerous set: the fuzzy matcher could land on
    a false option. Other dropdowns (experience, salary, notice) contain none of
    these and delegate untouched."""
    return any(d in str(t).lower() for t in (option_texts or []) for d in _DISQUALIFY)


def safe_eligibility_pick(options, hint: str = "") -> str | None:
    """The truthful option for the candidate's actual visa (default: 485 Temporary
    Graduate, full work rights, no sponsorship). Excludes false-status options,
    other visa types, and restricted-work options; prefers an explicit 485/graduate
    option, then a temporary visa with work rights, then generic work-rights
    phrasing, then an explicit "Other". None if nothing truthful exists, in which
    case the caller must HOLD rather than guess."""
    h = (hint or "").lower()
    opts = [str(o) for o in (options or [])]

    def ok(o: str) -> bool:
        lo = o.lower()
        if any(d in lo for d in _DISQUALIFY):
            return False
        if any(w in lo and w not in h for w in _WRONG_VISA_TYPES):
            return False
        if any(r in lo for r in _RESTRICTED):
            return False
        return True

    eligible = [o for o in opts if ok(o)]
    if not eligible:
        return None
    low = [(o, o.lower()) for o in eligible]

    # 1. an option naming the candidate's actual visa subclass / type
    for o, lo in low:
        if any(t in lo for t in ("485", "subclass 485", "temporary graduate",
                                 "graduate visa", "post-study", "post study")):
            return o
    # 2. a temporary visa WITH work rights (485-consistent)
    for o, lo in low:
        if "temporary visa" in lo and any(
            w in lo for w in ("allows me to work", "allow me to work",
                              "no restriction", "entitled to work", "right to work")
        ):
            return o
    # 3. generic work-rights phrasing not tied to a specific (wrong) visa type
    for o, lo in low:
        if any(w in lo for w in ("allows me to work", "right to work",
                                 "entitled to work", "any employer", "work right",
                                 "working right", "right to live and work")):
            return o
    # 4. an explicit "Other" is truthful when the 485 is not listed
    for o, lo in low:
        if lo.strip() in ("other", "other - please specify", "other (please specify)") or (
            "other" in lo and "visa" in lo
        ):
            return o
    # 5. don't guess a specific visa type
    return None


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
        if not hasattr(mod, "_answer_field"):
            return
        original = mod._answer_field  # type: ignore[attr-defined]

        async def _patched(page, q, job, candidate):
            tag = q.get("tag")
            options = q.get("options") or []
            label = q.get("label") or q.get("placeholder") or q.get("sectionText") or ""
            if tag == "SELECT" and options:
                texts = [str(o.get("text", "")) for o in options]
                if is_eligibility_question(label) or is_eligibility_option_set(texts):
                    hint = str(getattr(mod, "CAND_VISA_LABEL", "") or "")
                    pick = safe_eligibility_pick(texts, hint)
                    if pick is not None:
                        chosen = next(
                            (o for o in options if str(o.get("text", "")) == pick), None
                        )
                        logger.info(
                            "Eligibility guard: answering %r with %r (visa=%r)",
                            str(label)[:80],
                            pick,
                            hint,
                        )
                        await mod._safe_select_option(  # type: ignore[attr-defined]
                            page,
                            q.get("id"),
                            desired_value=(chosen or {}).get("value"),
                            desired_text=(chosen or {}).get("text"),
                        )
                        return
                    logger.warning(
                        "Eligibility guard: no truthful option for %r among %r; "
                        "HOLDING for the user (visa=%r)",
                        str(label)[:80],
                        texts,
                        hint,
                    )
                    raise QuestionHeldError(job_id_from_listing(job), str(label) or "work rights")
            return await original(page, q, job, candidate)

        self._mod = mod
        self._original = original
        mod._answer_field = _patched  # type: ignore[attr-defined]
        self._installed = True
        logger.info("EligibilityAnswerGuard installed on %s", self._module_name)

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            if self._mod is not None:
                self._mod._answer_field = self._original  # type: ignore[attr-defined]
        except Exception:
            pass
        self._installed = False

    def __enter__(self) -> "EligibilityAnswerGuard":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()
