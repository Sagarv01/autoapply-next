# tests/test_matcher.py
import pytest
from unittest.mock import AsyncMock, patch
from models import JobListing

JOB = JobListing(
    url="https://au.seek.com/job/1",
    title="DevOps Engineer",
    company="Acme",
    board="seek",
    description="AWS, Terraform, Kubernetes, CI/CD, Python required.",
)


@pytest.fixture(autouse=True)
def mock_profile(tmp_path, monkeypatch):
    profile = tmp_path / "profile.txt"
    profile.write_text("Sagar Verma. AWS, Terraform, Kubernetes, Python, Amazon Connect.")
    # The profile cache lives in utils now; reset and redirect.
    monkeypatch.setattr("utils._PROFILE_PATH", profile)
    monkeypatch.setattr("utils._profile_cache", None)


@pytest.mark.asyncio
async def test_score_job_returns_score_and_reasoning():
    import matcher
    reply = '{"score": 82, "reasoning": "Strong AWS and Terraform match."}'
    with patch("matcher.claude_complete", new=AsyncMock(return_value=reply)):
        score, reasoning = await matcher.score_job(JOB)
    assert score == 82
    assert "Terraform" in reasoning


@pytest.mark.asyncio
async def test_score_job_extracts_json_when_wrapped_in_fences():
    """Sonnet sometimes wraps JSON in ```json fences despite the prompt."""
    import matcher
    reply = '```json\n{"score": 70, "reasoning": "OK match."}\n```'
    with patch("matcher.claude_complete", new=AsyncMock(return_value=reply)):
        score, reasoning = await matcher.score_job(JOB)
    assert score == 70
    assert reasoning == "OK match."


@pytest.mark.asyncio
async def test_score_job_retries_on_error():
    import matcher
    call_count = 0

    async def flaky(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise Exception("CLI error")
        return '{"score": 70, "reasoning": "OK match."}'

    with patch("matcher.claude_complete", new=flaky):
        score, _ = await matcher.score_job(JOB)
    assert score == 70
    assert call_count == 2


@pytest.mark.asyncio
async def test_score_job_raises_after_max_retries():
    import matcher
    with patch("matcher.claude_complete", new=AsyncMock(side_effect=Exception("fail"))):
        with pytest.raises(Exception):
            await matcher.score_job(JOB)
