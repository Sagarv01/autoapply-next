"""Monkey-patch the vendored engine's LLM seam to route through the proxy.

The engine's single LLM chokepoint is `vendor/job-finder/claude_cli.claude_complete`,
imported by name into `matcher`, `tailorer`, and `seek_apply` (LinkedIn's
`linkedin_a11y_apply` is intentionally excluded as out of sprint scope). We do
NOT edit `vendor/`. Instead, exactly like `EngineHooks` patches
`tailorer._export_cover_letter_pdf`, this swaps each module's bound
`claude_complete` with a proxy-backed wrapper for the duration of a run and
restores it on exit. With it installed, the apply path makes zero `claude -p`
subprocess calls (verified by TASKS 2.4).
"""
from __future__ import annotations

import importlib
import logging
from contextlib import AbstractContextManager
from typing import Any

from . import llm_proxy

logger = logging.getLogger(__name__)

# In-scope modules that `from claude_cli import claude_complete`. LinkedIn is
# excluded on purpose (out of scope).
_TARGET_MODULES = ("matcher", "tailorer", "seek_apply")


async def _proxy_claude_complete(
    *, system: str, user: str, model: str | None = None, timeout: float = 180.0
) -> str:
    """Drop-in replacement for `claude_cli.claude_complete` that routes through
    the proxy. Signature-compatible with every engine call site."""
    return await llm_proxy.proxy_complete(system=system, user=user, model=model, timeout=timeout)


class ProxyLLM(AbstractContextManager):
    """Install the proxy LLM seam on the engine modules for one run.

    Use as a context manager (like `EngineHooks`). Idempotent install/uninstall;
    a module that is not importable or does not bind `claude_complete` is simply
    skipped, so this is safe in minimal/mocked test environments.
    """

    def __init__(self, modules: tuple[str, ...] = _TARGET_MODULES):
        self._module_names = modules
        self._originals: dict[str, Any] = {}
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        for name in self._module_names:
            try:
                mod = importlib.import_module(name)
            except Exception:
                continue
            if hasattr(mod, "claude_complete"):
                self._originals[name] = mod.claude_complete  # type: ignore[attr-defined]
                mod.claude_complete = _proxy_claude_complete  # type: ignore[attr-defined]
        self._installed = True
        logger.info("ProxyLLM installed on %s", sorted(self._originals))

    def uninstall(self) -> None:
        if not self._installed:
            return
        for name, original in self._originals.items():
            try:
                mod = importlib.import_module(name)
                mod.claude_complete = original  # type: ignore[attr-defined]
            except Exception:
                pass
        self._originals.clear()
        self._installed = False
        logger.info("ProxyLLM uninstalled")

    def __enter__(self) -> "ProxyLLM":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()
