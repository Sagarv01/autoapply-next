"""OptionalCoverLetter: make the Seek cover-letter upload optional.

A Free/Basic user with no base cover letter has no cover PDF for the apply. The
vendored seek_apply._upload_cover_letter always uploads, and an empty cover path
resolves to the working directory, which Playwright rejects ("File input does not
support directories"). This wraps that function for the duration of a run (no
vendor edit, exactly like ScreeningInterceptor/ProxyLLM) and SKIPS the upload when
there is no real cover, so the apply continues resume-only.
"""
from __future__ import annotations

import importlib
import logging
import os
from contextlib import AbstractContextManager

logger = logging.getLogger(__name__)


class OptionalCoverLetter(AbstractContextManager):
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
        if not hasattr(mod, "_upload_cover_letter"):
            return

        original = mod._upload_cover_letter  # type: ignore[attr-defined]

        async def _patched(page, cover_path, cover_name):
            # No real cover: empty name, or the empty path resolved to a directory.
            if not cover_name or os.path.isdir(str(cover_path)):
                logger.info("No cover letter provided; applying resume-only.")
                return None
            return await original(page, cover_path, cover_name)

        self._original = original
        mod._upload_cover_letter = _patched  # type: ignore[attr-defined]
        self._installed = True
        logger.info("OptionalCoverLetter installed on %s", self._module_name)

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            mod = importlib.import_module(self._module_name)
            mod._upload_cover_letter = self._original  # type: ignore[attr-defined]
        except Exception:
            pass
        self._installed = False

    def __enter__(self) -> "OptionalCoverLetter":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()
