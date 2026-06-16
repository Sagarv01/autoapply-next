"""Answering a held screening question, end to end.

One place that ties the three effects together so the UI (and tests) get them all:
  1. remember the answer in the held-queue bank, so the resolver reuses it on
     FUTURE jobs (not just the ones currently waiting),
  2. unblock every job that was waiting on that exact question, and
  3. re-queue those parked jobs in jobs.db ('held' -> 'queued') so the next batch
     re-applies them with the remembered answer.

Pure orchestration; the UI runs it off the GUI thread via the AsyncTaskRunner.
"""
from __future__ import annotations

from pathlib import Path

from ..engine import persistence
from .held_queue import HeldQueue


def _held_path(engine_workdir) -> Path:
    return Path(engine_workdir) / "held_questions.json"


def answer_and_unblock(engine_workdir, question: str, answer: str) -> dict:
    """Record `answer` for `question` and unblock everything waiting on it.

    Returns {"unblocked": [job_id, ...], "requeued": int}. Unknown question -> a
    no-op ({"unblocked": [], "requeued": 0})."""
    held_path = _held_path(engine_workdir)
    held = HeldQueue.load(held_path)
    unblocked = held.answer(question, answer)  # remembers + returns unblocked ids
    held.save(held_path)                        # persist the bank
    requeued = persistence.requeue_held_jobs(engine_workdir, unblocked)
    return {"unblocked": unblocked, "requeued": requeued}
