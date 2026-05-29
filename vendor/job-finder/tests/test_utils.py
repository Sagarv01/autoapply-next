# tests/test_utils.py
import pytest
from unittest.mock import patch
from utils import sanitise_name, make_filename, retry_async
import asyncio

def test_sanitise_name_spaces():
    assert sanitise_name("DevOps Engineer") == "DevOpsEngineer"

def test_sanitise_name_special_chars():
    assert sanitise_name("A & B (Pty) Ltd") == "ABPtyLtd"

def test_make_filename_resume():
    name = make_filename("SagarVerma", "Atlassian", "DevOps Engineer")
    assert name.startswith("SagarVerma_Atlassian_DevOpsEngineer_")
    assert name.endswith(".pdf")

def test_make_filename_no_collision():
    from unittest.mock import patch
    from datetime import datetime as _dt
    dt1 = _dt(2026, 4, 1, 14, 32, 11, 42000)   # microsecond=42000 → ms=042
    dt2 = _dt(2026, 4, 1, 14, 32, 11, 43000)   # microsecond=43000 → ms=043
    with patch("utils.datetime") as mock_dt:
        mock_dt.now.side_effect = [dt1, dt2]
        mock_dt.now.return_value = dt1
        mock_dt.now.side_effect = [dt1, dt2]
        n1 = make_filename("SagarVerma", "Acme", "Engineer")
        n2 = make_filename("SagarVerma", "Acme", "Engineer")
    assert n1 != n2
    assert "042" in n1
    assert "043" in n2

@pytest.mark.asyncio
async def test_retry_async_succeeds_on_first_try():
    calls = []
    async def fn():
        calls.append(1)
        return "ok"
    result = await retry_async(fn)
    assert result == "ok"
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_retry_async_retries_on_failure():
    calls = []
    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("fail")
        return "ok"
    result = await retry_async(fn, max_retries=3, base_delay=0.01)
    assert result == "ok"
    assert len(calls) == 3

@pytest.mark.asyncio
async def test_retry_async_raises_after_max():
    async def fn():
        raise ValueError("always fails")
    with pytest.raises(ValueError):
        await retry_async(fn, max_retries=2, base_delay=0.01)
