"""TASKS 2.4: the engine runs with the claude CLI absent from PATH.

With ProxyLLM installed (TASKS 2.1), the real matcher.score_job routes its LLM
call through the proxy, never the CLI. This test removes `claude` from PATH,
guards against ANY `claude` subprocess being spawned, mocks the proxy transport,
and asserts the real engine scoring completes and returns the proxy's result.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from autoapply_next.engine import llm_proxy
from autoapply_next.engine.llm_adapter import ProxyLLM


class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.mark.requires_engine
async def test_real_score_job_runs_without_claude_via_proxy(engine_workdir, monkeypatch):
    # 1. claude is absent from PATH.
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which",
        lambda name, *a, **k: None if name == "claude" else real_which(name, *a, **k),
    )
    assert shutil.which("claude") is None

    # 2. No `claude` subprocess may be spawned on the apply path.
    real_exec = asyncio.create_subprocess_exec

    async def _guard_exec(*args, **kwargs):
        if args and "claude" in str(args[0]):
            raise AssertionError(f"claude subprocess spawned: {args[0]!r}")
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _guard_exec)

    # 3. The proxy answers the scoring call.
    llm_proxy.set_access_token_provider(lambda: "test-jwt")

    async def _fake_post(url, headers, payload, timeout):
        assert url.endswith("/api/llm/complete")
        return _FakeResp(200, {"text": '{"score": 80, "reasoning": "strong platform match"}'})

    monkeypatch.setattr(llm_proxy, "_http_post", _fake_post)

    try:
        import matcher  # real vendored engine module (engine_workdir put it on sys.path)
        from models import JobListing

        job = JobListing(
            url="https://www.seek.com.au/job/123",
            title="Platform Engineer",
            company="Acme",
            board="seek",
            description="Kubernetes, Terraform, AWS platform role.",
        )
        with ProxyLLM():
            score, reasoning = await matcher.score_job(job)

        assert score == 80
        assert "platform" in reasoning.lower()
    finally:
        llm_proxy.set_access_token_provider(None)
