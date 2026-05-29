"""Runtime hooks that capture engine internals for the GUI's results screen.

These are observability hooks, not safety. They are split from `safety.py` so a
reader can see at a glance which patches matter for "would this submit a real
application" (none here) and which patches matter for "what did the engine
produce for me to review" (all here).

Two pieces of data the engine does not return but the GUI needs to show:

1. Cover letter body text. `tailorer.tailor()` returns only the PDF paths. The
   cover letter text flows through `tailorer._export_cover_letter_pdf(text, job)`
   as the first positional argument; we wrap that function to capture it before
   delegating to the original.

2. Screening question answers. `seek_apply._Journal` writes a JSON line to
   `errors/applications.jsonl` at the end of each apply run, including
   `questions_answered`. We read the last line of that file after apply
   finishes. This avoids patching `_Journal` and means we get whatever the
   engine actually decided to record.

Both pieces of data are user-facing (cover letter goes out under the user's
name; screening answers similarly). They must be surfaced before any
allow_real_submit toggle.
"""

from __future__ import annotations

import json
import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CapturedRun:
    """Per-run capture, reset on each enter()."""

    cover_letter_text: str | None = None
    screening_answers: list[dict] = field(default_factory=list)
    journal_outcome: str | None = None
    journal_final_error: str | None = None


class EngineHooks(AbstractContextManager):
    """Install observability patches on the engine for the duration of one run.

    Use as a context manager. Read `hooks.captured` after exit (the data lives
    on after uninstall so the adapter can build the ApplicationResult).
    """

    def __init__(self, *, journal_path: Path):
        self._journal_path = Path(journal_path)
        self._captured = CapturedRun()
        self._original_export: Any = None
        self._installed = False

    @property
    def captured(self) -> CapturedRun:
        return self._captured

    def install(self) -> None:
        if self._installed:
            return
        import tailorer  # type: ignore[import-not-found]

        captured = self._captured
        original = tailorer._export_cover_letter_pdf

        def captured_export(cover_text, job):
            captured.cover_letter_text = cover_text
            return original(cover_text, job)

        tailorer._export_cover_letter_pdf = captured_export  # type: ignore[attr-defined]
        self._original_export = original
        self._installed = True
        logger.info("EngineHooks installed")

    def uninstall(self) -> None:
        if not self._installed:
            return
        import tailorer  # type: ignore[import-not-found]

        tailorer._export_cover_letter_pdf = self._original_export  # type: ignore[attr-defined]
        self._installed = False
        logger.info("EngineHooks uninstalled")

    def read_last_journal(self) -> None:
        """Read the last line of the engine's applications.jsonl (if present)
        and populate `captured.screening_answers` / `journal_outcome` /
        `journal_final_error`.

        Called by the adapter after `applicator.apply` returns or raises.
        Failures here are logged but never re-raised: missing journal is not a
        user-facing problem.
        """
        if not self._journal_path.exists():
            return
        try:
            # The file can be megabytes if many applies happen. Read backwards
            # to grab the last line only. For now, the file is small (~100 KB),
            # so a single read is fine; revisit if it grows.
            with open(self._journal_path, "rb") as f:
                f.seek(0, 2)  # end
                file_size = f.tell()
                read_size = min(file_size, 32 * 1024)  # last 32 KB suffices
                f.seek(file_size - read_size)
                tail = f.read().decode("utf-8", errors="replace")
            lines = [ln for ln in tail.splitlines() if ln.strip()]
            if not lines:
                return
            last = json.loads(lines[-1])
        except Exception as exc:
            logger.warning("EngineHooks.read_last_journal failed: %s", exc)
            return
        self._captured.screening_answers = last.get("questions_answered") or []
        self._captured.journal_outcome = last.get("outcome")
        self._captured.journal_final_error = last.get("final_error")

    # ---------------------------------------------------------- context mgr

    def __enter__(self) -> "EngineHooks":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()
