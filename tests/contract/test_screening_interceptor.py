"""M-C: intercept the engine's screening answerer before it guesses.

seek_apply._claude_answer is the "unknown field -> ask Claude" seam (line 923-938).
The interceptor wraps it (no vendor edit, like ProxyLLM/EngineHooks) and consults
the resolver: a remembered answer is returned without an LLM call, a fact question
is delegated to the original, and an unknown question is held + the job's apply is
aborted with QuestionHeldError (so it never submits a guess).
"""

from __future__ import annotations

import sys
import types

import pytest

from autoapply_next.screening.held_queue import HeldQueue
from autoapply_next.screening.interceptor import (
    QuestionHeldError,
    ScreeningInterceptor,
    job_id_from_listing,
)


class _Job:
    def __init__(self, url="https://www.seek.com.au/job/12345678", title="Eng", company="Acme"):
        self.url = url
        self.title = title
        self.company = company


def _fake_seek_apply(calls: list) -> types.ModuleType:
    m = types.ModuleType("seek_apply")

    async def _claude_answer(question, options, job, model=None):
        calls.append((question, options))
        return "ENGINE-ANSWER"

    m._claude_answer = _claude_answer  # type: ignore[attr-defined]
    return m


def test_job_id_from_listing_uses_seek_url_tail():
    assert job_id_from_listing(_Job(url="https://www.seek.com.au/job/87654321?type=promoted")) == "87654321"
    assert job_id_from_listing(_Job(url="", title="T", company="C")) == "T|C"


async def test_delegates_fact_question_to_original(monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(calls))
    with ScreeningInterceptor(HeldQueue()):
        out = await sys.modules["seek_apply"]._claude_answer(
            "Do you have the right to work in Australia?", None, _Job()
        )
    assert out == "ENGINE-ANSWER"
    assert len(calls) == 1


async def test_unknown_question_is_held_and_aborts(monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(calls))
    held = HeldQueue()
    with ScreeningInterceptor(held):
        with pytest.raises(QuestionHeldError) as ei:
            await sys.modules["seek_apply"]._claude_answer(
                "Why do you want to work here?", None, _Job(url="https://www.seek.com.au/job/999")
            )
    assert ei.value.job_id == "999"
    assert calls == []  # original never called -> no guess
    assert held.is_blocked("999")
    assert [e.question for e in held.pending()] == ["Why do you want to work here?"]


async def test_remembered_answer_returned_without_calling_original(monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(calls))
    held = HeldQueue()
    held.hold("seed", "Do you have a police check?")
    held.answer("Do you have a police check?", "Yes, until 2027")
    with ScreeningInterceptor(held):
        out = await sys.modules["seek_apply"]._claude_answer(
            "do you have a police check", None, _Job()
        )
    assert out == "Yes, until 2027"
    assert calls == []


async def test_options_recorded_as_single_select(monkeypatch):
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply([]))
    held = HeldQueue()
    with ScreeningInterceptor(held):
        with pytest.raises(QuestionHeldError):
            await sys.modules["seek_apply"]._claude_answer(
                "Choose your shirt size", ["S", "M", "L"], _Job(url="https://www.seek.com.au/job/55")
            )
    entry = held.pending()[0]
    assert entry.options == ["S", "M", "L"]
    assert entry.input_type == "single_select"


async def test_hold_is_persisted_when_save_path_given(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply([]))
    path = tmp_path / "held.json"
    held = HeldQueue()
    with ScreeningInterceptor(held, save_path=path):
        with pytest.raises(QuestionHeldError):
            await sys.modules["seek_apply"]._claude_answer(
                "Describe your ideal team", None, _Job(url="https://www.seek.com.au/job/77")
            )
    reloaded = HeldQueue.load(path)
    assert reloaded.is_blocked("77")


def test_install_restores_original(monkeypatch):
    calls: list = []
    fake = _fake_seek_apply(calls)
    monkeypatch.setitem(sys.modules, "seek_apply", fake)
    original = fake._claude_answer
    interceptor = ScreeningInterceptor(HeldQueue())
    interceptor.install()
    assert fake._claude_answer is not original
    interceptor.uninstall()
    assert fake._claude_answer is original


def test_question_held_error_is_baseexception_not_exception():
    # Must propagate past the vendored engine's `except Exception` around
    # _claude_answer (seek_apply.py:1076), exactly like asyncio.CancelledError.
    assert issubclass(QuestionHeldError, BaseException)
    assert not issubclass(QuestionHeldError, Exception)


async def test_held_error_survives_a_broad_except_exception(monkeypatch):
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply([]))
    with ScreeningInterceptor(HeldQueue()):
        outcome = "not-raised"
        try:
            await sys.modules["seek_apply"]._claude_answer(
                "An unknown question?", None, _Job(url="https://www.seek.com.au/job/9")
            )
        except Exception:  # noqa: BLE001 - this is the swallow the engine would do
            outcome = "swallowed"
        except QuestionHeldError:
            outcome = "propagated"
    assert outcome == "propagated"


def test_skips_module_without_claude_answer(monkeypatch):
    bare = types.ModuleType("seek_apply")
    monkeypatch.setitem(sys.modules, "seek_apply", bare)
    with ScreeningInterceptor(HeldQueue()):  # must not raise
        assert not hasattr(bare, "_claude_answer")
