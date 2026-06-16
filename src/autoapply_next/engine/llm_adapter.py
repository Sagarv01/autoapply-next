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

# Modules whose LLM work is per-job document tailoring (Pro-gated server-side).
# Only `tailorer` produces the tailored resume/cover; `matcher` (scoring) and
# `seek_apply` (screening answers) are generic completions open to every tier.
_TASK_BY_MODULE = {"tailorer": "tailor"}


def _make_proxy_claude_complete(task: str | None):
    """Build a drop-in `claude_cli.claude_complete` replacement bound to a proxy
    task hint. Signature-compatible with every engine call site (the engine never
    passes `task`; it is injected per-module here).

    Implements the 401 refresh-and-retry-once policy (TASKS 2.2): on an expired
    session, refresh the access token once and retry exactly one more time. A
    persistent 401 (or no refresher configured) propagates as AuthExpiredError,
    which is fatal-for-batch (see persistence.is_fatal_condition).
    """

    async def _wrapper(
        *, system: str, user: str, model: str | None = None, timeout: float = 180.0
    ) -> str:
        try:
            return await llm_proxy.proxy_complete(
                system=system, user=user, model=model, task=task, timeout=timeout
            )
        except llm_proxy.AuthExpiredError:
            refreshed = await llm_proxy.refresh_access_token()
            if not refreshed:
                raise
            return await llm_proxy.proxy_complete(
                system=system, user=user, model=model, task=task, timeout=timeout
            )

    return _wrapper


# Module-level singletons so the installer can swap by identity and tests can
# assert which wrapper landed on which module.
_proxy_claude_complete = _make_proxy_claude_complete(None)
_proxy_claude_complete_tailor = _make_proxy_claude_complete("tailor")


def _wrapper_for_module(name: str):
    return _proxy_claude_complete_tailor if _TASK_BY_MODULE.get(name) == "tailor" else _proxy_claude_complete


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
                mod.claude_complete = _wrapper_for_module(name)  # type: ignore[attr-defined]
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
