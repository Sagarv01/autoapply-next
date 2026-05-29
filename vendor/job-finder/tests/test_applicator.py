# tests/test_applicator.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from models import JobListing

JOB = JobListing(url="https://linkedin.com/jobs/1", title="DevOps Engineer",
                 company="Acme", board="linkedin", description="AWS required")
CANDIDATE = {"name": "Sagar Verma", "email": "sagar@email.com", "phone": "+61400000000"}


@pytest.mark.asyncio
async def test_apply_returns_applied_on_success():
    import applicator
    mock_agent = MagicMock()
    mock_result = MagicMock()
    mock_result.final_result.return_value = "Done. Application submitted."
    mock_agent.run = AsyncMock(return_value=mock_result)
    with patch("applicator.Agent", return_value=mock_agent):
        result = await applicator.apply(JOB, "output/resume.pdf", "output/cover.pdf", CANDIDATE)
    assert result == "applied"


@pytest.mark.asyncio
async def test_apply_raises_timeout():
    import asyncio, applicator
    mock_agent = MagicMock()
    async def hang():
        await asyncio.sleep(9999)
    mock_agent.run = hang
    with patch("applicator.Agent", return_value=mock_agent), \
         patch("applicator.APPLY_TIMEOUT", 0.01):
        with pytest.raises(TimeoutError):
            await applicator.apply(JOB, "output/resume.pdf", "output/cover.pdf", CANDIDATE)


@pytest.mark.asyncio
async def test_apply_raises_board_blocked_on_captcha():
    import applicator
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=Exception("CAPTCHA detected"))
    with patch("applicator.Agent", return_value=mock_agent):
        with pytest.raises(applicator.BoardBlockedError):
            await applicator.apply(JOB, "output/resume.pdf", "output/cover.pdf", CANDIDATE)
