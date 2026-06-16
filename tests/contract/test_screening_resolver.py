"""M-C: screening resolution policy.

For each screening question the resolver decides one of three things:
  - "answered": we already have the user's answer (remembered) -> use it,
  - "delegate": the question is answerable from the candidate's facts -> let the
    engine answer it from the profile,
  - "held": unknown -> add to the held queue and stop the engine guessing.
A remembered answer always wins, even for a fact-style question.
"""

from __future__ import annotations

import pytest

from autoapply_next.screening.held_queue import HeldQueue
from autoapply_next.screening.resolver import ScreeningResolver, is_answerable_from_facts


@pytest.mark.parametrize(
    "question",
    [
        "Are you an Australian citizen?",
        "Do you have permanent residency?",
        "Do you have the right to work in Australia?",
        "Are you authorised to work in Australia?",
        "What is your visa status?",
        "Do you hold a security clearance (NV1)?",
        "What are your salary expectations?",
        "What is your expected remuneration?",
        "What is your notice period?",
        "When are you available to start?",
        "What is your highest level of education?",
        "How many years of experience do you have with AWS?",
        "Where are you currently located?",
        "Are you willing to relocate?",
    ],
)
def test_fact_questions_are_answerable(question):
    assert is_answerable_from_facts(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "Why do you want to work for our company?",
        "Do you have a current driver's licence?",
        "Describe a time you resolved a conflict.",
        "Do you have a current police check?",
        "What is your favourite programming language and why?",
    ],
)
def test_non_fact_questions_are_not_answerable(question):
    assert is_answerable_from_facts(question) is False


def test_resolve_delegates_fact_question():
    r = ScreeningResolver(HeldQueue())
    res = r.resolve("job-1", "Do you have the right to work in Australia?")
    assert res.kind == "delegate"


def test_resolve_holds_unknown_and_queues_it():
    q = HeldQueue()
    r = ScreeningResolver(q)
    res = r.resolve("job-1", "Why do you want this role?", options=None, input_type="free_text")
    assert res.kind == "held"
    assert q.is_blocked("job-1")
    assert [e.question for e in q.pending()] == ["Why do you want this role?"]


def test_resolve_uses_remembered_answer_even_for_unknown():
    q = HeldQueue()
    q.hold("job-0", "Do you have a police check?")
    q.answer("Do you have a police check?", "Yes, valid until 2027")
    r = ScreeningResolver(q)
    res = r.resolve("job-1", "do you have a police check")
    assert res.kind == "answered"
    assert res.answer == "Yes, valid until 2027"
    # a remembered answer must not re-queue the job
    assert not q.is_blocked("job-1")


def test_remembered_answer_wins_over_fact_classification():
    q = HeldQueue()
    q.hold("job-0", "What is your notice period?")
    q.answer("What is your notice period?", "4 weeks")
    r = ScreeningResolver(q)
    res = r.resolve("job-1", "What is your notice period?")
    assert res.kind == "answered"
    assert res.answer == "4 weeks"
