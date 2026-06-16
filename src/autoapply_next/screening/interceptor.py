"""Engine interception for the held queue.

Wraps the vendored `seek_apply._claude_answer` (the "unknown field -> ask Claude"
seam) for the duration of a run, exactly like ProxyLLM/EngineHooks patch their
seams (no vendor edit). For each screening question the wrapper consults the
ScreeningResolver:

  - remembered answer  -> return it (no LLM call),
  - fact question      -> delegate to the original answerer,
  - unknown            -> hold it and raise QuestionHeldError to abort this job's
                          apply, so the engine never submits a guessed answer.

The caller (adapter) catches QuestionHeldError and marks the job 'held' rather
than 'failed'; answering the queued question later unblocks it.
"""
from __future__ import annotations

import importlib
import logging
from contextlib import AbstractContextManager

from .held_queue import HeldQueue
from .resolver import ScreeningResolver

logger = logging.getLogger(__name__)


class QuestionHeldError(BaseException):
    """Raised mid-apply when a screening question must be answered by the user.

    Aborts the current job's apply without submitting. Not a failure: the apply
    is held pending the user's answer.

    Inherits BaseException (not Exception) on purpose, exactly like
    asyncio.CancelledError: the vendored engine wraps its `_claude_answer` calls
    in `except Exception` and would otherwise SWALLOW this and fall through to a
    guessed answer (seek_apply.py:1076). As a BaseException it propagates cleanly
    past those handlers up to the adapter, which turns it into a HELD result."""

    def __init__(self, job_id: str, question: str):
        super().__init__(f"held screening question for job {job_id}: {question!r}")
        self.job_id = job_id
        self.question = question


def job_id_from_listing(job) -> str:
    """The stable job id, matching the engine's own derivation
    (`job.url.rstrip('/').split('/')[-1].split('?')[0]`). Falls back to
    title|company when there is no URL."""
    url = getattr(job, "url", "") or ""
    if url:
        return url.rstrip("/").split("/")[-1].split("?")[0]
    return f"{getattr(job, 'title', '')}|{getattr(job, 'company', '')}".strip("|")


class ScreeningInterceptor(AbstractContextManager):
    """Install the held-queue interception on seek_apply for one run."""

    def __init__(self, held: HeldQueue, *, module_name: str = "seek_apply", save_path=None):
        self._held = held
        self._resolver = ScreeningResolver(held)
        self._module_name = module_name
        self._save_path = save_path
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
        resolver = self._resolver
        held = self._held
        save_path = self._save_path

        async def _intercepted(question, options, job, model=None):
            job_id = job_id_from_listing(job)
            res = resolver.resolve(
                job_id,
                question,
                options=list(options) if options else None,
                input_type="single_select" if options else "free_text",
            )
            if res.kind == "answered":
                return res.answer
            if res.kind == "held":
                if save_path is not None:
                    try:
                        held.save(save_path)
                    except Exception:  # noqa: BLE001 - persistence must not break apply
                        logger.warning("held queue save failed", exc_info=True)
                raise QuestionHeldError(job_id, question)
            return await original(question, options, job, model)

        self._original = original
        mod._claude_answer = _intercepted  # type: ignore[attr-defined]
        self._installed = True
        logger.info("ScreeningInterceptor installed on %s", self._module_name)

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            mod = importlib.import_module(self._module_name)
            mod._claude_answer = self._original  # type: ignore[attr-defined]
        except Exception:
            pass
        self._installed = False
        logger.info("ScreeningInterceptor uninstalled")

    def __enter__(self) -> "ScreeningInterceptor":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()
