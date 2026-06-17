"""EligibilityAnswerGuard: never falsely claim citizenship / PR on a work-rights
question.

Seek SELECT (dropdown) screening questions bypass the engine's radio hard-rule and
go straight to the LLM, which has been observed to pick "I'm an Australian citizen"
on the standard "right to work" question (AU_Q_6) for a candidate who is NOT a
citizen (485 visa, full work rights). A false eligibility claim must never reach a
real submit.

This wraps seek_apply._claude_answer (no vendor edit, like ScreeningInterceptor):
for an eligibility question WITH options, it deterministically picks a SAFE option
and never a disqualified one (citizen / permanent resident / NZ citizen / requires
sponsorship / student / working-holiday / not entitled). It prefers the option
that states a visa with work rights. Only if no safe option exists does it fall
through to the original answerer.
"""
from __future__ import annotations

import importlib
import logging
from contextlib import AbstractContextManager

logger = logging.getLogger(__name__)

# Options that are factually untrue for a non-citizen 485 holder, or that would
# misrepresent their status. Never pick these on a work-rights question.
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
        self._original = None
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        try:
            mod = importlib.import_module(self._module_name)
        except Exception:
            return
        if not hasattr(mod, "_claude_answer"):
            return
        original = mod._claude_answer  # type: ignore[attr-defined]

        async def _patched(question, options, job, model=None):
            if options and is_eligibility_question(question):
                pick = safe_eligibility_pick(options)
                if pick is not None:
                    logger.info(
                        "Eligibility guard: answered %r with %r (never claims citizenship)",
                        (question or "")[:70],
                        pick,
                    )
                    return pick
            return await original(question, options, job, model)

        self._original = original
        mod._claude_answer = _patched  # type: ignore[attr-defined]
        self._installed = True
        logger.info("EligibilityAnswerGuard installed on %s", self._module_name)

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            mod = importlib.import_module(self._module_name)
            mod._claude_answer = self._original  # type: ignore[attr-defined]
        except Exception:
            pass
        self._installed = False

    def __enter__(self) -> "EligibilityAnswerGuard":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()
