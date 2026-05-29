"""Wrap `claude -p` so the bot uses the Max subscription instead of API billing.

The Claude Code CLI prefers ANTHROPIC_API_KEY over OAuth when the env var is
set, so we strip that (and related provider flags) from the subprocess env.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeCLIError(RuntimeError):
    """Raised when `claude -p` exits non-zero or returns no usable output."""


_API_AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def _build_subscription_env() -> dict[str, str]:
    env = os.environ.copy()
    for v in _API_AUTH_VARS:
        env.pop(v, None)
    return env


_SUBSCRIPTION_ENV = _build_subscription_env()


async def claude_complete(
    *,
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 180.0,
) -> str:
    """Single-shot text completion via `claude -p` on the user's subscription.
    Returns the stripped reply. Raises ClaudeCLIError on non-zero exit or
    empty stdout. Tools disabled; sessions not persisted.
    """
    cli = shutil.which("claude")
    if cli is None:
        raise ClaudeCLIError(
            "`claude` CLI not found on PATH. Install Claude Code and run `claude login`."
        )
    args = [
        cli, "-p",
        "--model", model,
        "--system-prompt", system,
        "--tools", "",
        "--no-session-persistence",
        "--output-format", "text",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_SUBSCRIPTION_ENV,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=user.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ClaudeCLIError(f"`claude -p` timed out after {timeout}s")

    out = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")

    if proc.returncode != 0:
        # Rate-limit / quota / auth notices sometimes land on stdout, not stderr.
        raise ClaudeCLIError(
            f"`claude -p` exit={proc.returncode} "
            f"stderr={err.strip()[:500]!r} stdout={out.strip()[:500]!r}"
        )

    text = out.strip()
    if not text:
        raise ClaudeCLIError(
            f"`claude -p` returned empty stdout. stderr={err.strip()[:500]!r}"
        )
    return text
