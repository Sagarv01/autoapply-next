"""Screening resolution policy: answer / delegate / hold.

`is_answerable_from_facts` recognizes the questions the engine can answer from the
candidate's profile (the same facts its prompt carries: citizenship/PR, right to
work, visa, security clearance, salary, notice/availability, education, years of
experience, location). Anything outside that set is treated as unknown.

`ScreeningResolver.resolve` is the per-question decision the engine interception
consults: a remembered user answer wins; else a fact question is delegated to the
engine; else it is held (queued) so the engine never guesses and submits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .held_queue import HeldQueue, normalize_question

# Single-keyword/phrase triggers (matched as substrings of the normalized text).
_FACT_PATTERNS: tuple[str, ...] = (
    "citizen",
    "permanent residen",  # residency / resident
    "right to work",
    "work rights",
    "authorised to work",
    "authorized to work",
    "legally work",
    "eligible to work",
    "work in australia",
    "visa",
    "security clearance",
    "clearance",
    "nv1",
    "nv2",
    "baseline",
    "agsva",
    "salary",
    "remuneration",
    "compensation",
    "expected pay",
    "pay expectation",
    "day rate",
    "hourly rate",
    "notice period",
    "available to start",
    "availability",
    "when can you start",
    "start date",
    "immediately available",
    "highest level of education",
    "highest education",
    "qualification",
    "degree",
    "currently located",
    "where are you based",
    "willing to relocate",
    "relocate",
    "relocation",
    "employment status",
)

# Co-occurrence triggers: both terms present anywhere in the question.
_FACT_PAIRS: tuple[tuple[str, str], ...] = (
    ("year", "experience"),  # "how many years of X experience"
    ("how many years", "experience"),
)


def is_answerable_from_facts(question: str) -> bool:
    """True if the engine can answer this from the candidate's profile facts."""
    q = normalize_question(question)
    if any(p in q for p in _FACT_PATTERNS):
        return True
    return any(a in q and b in q for a, b in _FACT_PAIRS)


@dataclass
class Resolution:
    kind: str  # "answered" | "delegate" | "held"
    answer: str | None = None


class ScreeningResolver:
    """Decide how each screening question should be handled, backed by the held
    queue (remembered answers + holding unknowns)."""

    def __init__(self, held: HeldQueue):
        self._held = held

    def resolve(
        self,
        job_id: str,
        question: str,
        *,
        options: list[str] | None = None,
        input_type: str = "free_text",
    ) -> Resolution:
        remembered = self._held.known_answer(question)
        if remembered is not None:
            return Resolution("answered", answer=remembered)
        if is_answerable_from_facts(question):
            return Resolution("delegate")
        self._held.hold(job_id, question, options=options, input_type=input_type)
        return Resolution("held")
