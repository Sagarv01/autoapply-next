"""The unknown-question held queue.

When the engine meets a screening question it can't answer from the candidate's
facts, the app holds the application instead of letting the engine guess and
submit. Each unique question (deduped by a normalized key) is queued once, with
the set of jobs waiting on it. Answering a question:
  - remembers the answer (so the same question is never asked again), and
  - unblocks every job that was only waiting on that question.

State is a small JSON file in the engine workdir, so a held queue survives an app
restart. Pure data model with no Qt/engine dependency, so it is testable headless;
the engine interception and the "Waiting on you" UI wrap this.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_STRIP = re.compile(r"[\s\?\*\.\:,;!]+$")


def normalize_question(question: str) -> str:
    """Dedup key for a question: lowercased, whitespace-collapsed, trailing
    punctuation removed. So differing case/spacing/'?'/'*' map to one entry."""
    q = _WS.sub(" ", (question or "").strip().lower())
    return _STRIP.sub("", q).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HeldQuestion:
    key: str
    question: str  # the original (display) text
    input_type: str = "free_text"
    options: list[str] | None = None
    job_ids: list[str] = field(default_factory=list)
    status: str = "waiting"  # "waiting" | "answered"
    answer: str | None = None
    created_at: str = field(default_factory=_now)
    answered_at: str | None = None

    def add_job(self, job_id: str) -> None:
        if job_id not in self.job_ids:
            self.job_ids.append(job_id)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "question": self.question,
            "input_type": self.input_type,
            "options": self.options,
            "job_ids": list(self.job_ids),
            "status": self.status,
            "answer": self.answer,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HeldQuestion":
        return cls(
            key=d["key"],
            question=d.get("question", ""),
            input_type=d.get("input_type", "free_text"),
            options=d.get("options"),
            job_ids=list(d.get("job_ids") or []),
            status=d.get("status", "waiting"),
            answer=d.get("answer"),
            created_at=d.get("created_at") or _now(),
            answered_at=d.get("answered_at"),
        )


class HeldQueue:
    """A dedup-by-question store of held screening questions + remembered answers."""

    def __init__(self, entries: dict[str, HeldQuestion] | None = None):
        self._entries: dict[str, HeldQuestion] = entries or {}

    # ------------------------------------------------------------- holding
    def hold(
        self,
        job_id: str,
        question: str,
        *,
        options: list[str] | None = None,
        input_type: str = "free_text",
    ) -> HeldQuestion:
        """Register `job_id` as waiting on `question`, creating the queue entry
        on first sight. Returns the (possibly pre-existing) entry."""
        key = normalize_question(question)
        entry = self._entries.get(key)
        if entry is None:
            entry = HeldQuestion(
                key=key, question=question, input_type=input_type, options=options
            )
            self._entries[key] = entry
        else:
            # keep the richest metadata we've seen
            if options and not entry.options:
                entry.options = options
            if input_type and entry.input_type == "free_text":
                entry.input_type = input_type
        entry.add_job(job_id)
        return entry

    # ------------------------------------------------------------ answering
    def answer(self, question: str, answer: str) -> list[str]:
        """Record the answer to a held question and return the job ids that are
        now fully unblocked (no remaining waiting questions). Unknown question
        -> no-op, returns []."""
        key = normalize_question(question)
        entry = self._entries.get(key)
        if entry is None:
            return []
        entry.status = "answered"
        entry.answer = answer
        entry.answered_at = _now()
        return [jid for jid in entry.job_ids if not self.is_blocked(jid)]

    def known_answer(self, question: str) -> str | None:
        entry = self._entries.get(normalize_question(question))
        if entry is not None and entry.status == "answered":
            return entry.answer
        return None

    # ------------------------------------------------------------- queries
    def pending(self) -> list[HeldQuestion]:
        return [e for e in self._entries.values() if e.status == "waiting"]

    def is_blocked(self, job_id: str) -> bool:
        return any(job_id in e.job_ids for e in self._entries.values() if e.status == "waiting")

    def questions_for_job(self, job_id: str) -> list[HeldQuestion]:
        return [
            e
            for e in self._entries.values()
            if e.status == "waiting" and job_id in e.job_ids
        ]

    # ---------------------------------------------------------- persistence
    def save(self, path) -> None:
        payload = {"version": 1, "entries": [e.to_dict() for e in self._entries.values()]}
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "HeldQueue":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
            entries = {
                d["key"]: HeldQuestion.from_dict(d) for d in data.get("entries", [])
            }
            return cls(entries)
        except Exception as exc:  # noqa: BLE001 - a corrupt file must not brick the app
            logger.warning("HeldQueue.load failed (%s); starting empty", exc)
            return cls()
