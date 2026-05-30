"""pytest-qt tests for EngineWorker.scrape_and_score.

Mocks `scrape_and_score` in the worker so we test the wiring without
launching a browser. Verifies log streaming, scrape_finished delivery, and
cancellation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from autoapply_next.engine import worker as worker_module
from autoapply_next.engine.scraping import ScrapeResult, ScrapedJob
from autoapply_next.engine.worker import EngineWorker


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def _build_worker(workdir: Path) -> EngineWorker:
    return EngineWorker(engine_workdir=workdir)


def _fake_result(keyword: str) -> ScrapeResult:
    return ScrapeResult(
        keyword=keyword,
        total_scraped=5,
        new_jobs=3,
        scored=[
            ScrapedJob(
                url=f"https://au.seek.com/job/{i}",
                title=f"Test {i}",
                company="Acme",
                score=60 + i,
                reasoning="ok",
                description_chars=500,
            )
            for i in range(3)
        ],
        errors=[],
    )


def test_scrape_progress_then_finished(qtbot, workdir, monkeypatch):
    async def fake(*, keyword, engine_workdir, location="Australia",
                   on_status=None, is_cancelled=None, max_jobs=12):
        on_status("scraping")
        await asyncio.sleep(0.01)
        on_status("scoring")
        return _fake_result(keyword)

    monkeypatch.setattr(worker_module, "scrape_and_score", fake)

    worker = _build_worker(workdir)
    try:
        logs: list[str] = []
        finished: list[ScrapeResult] = []
        worker.log.connect(logs.append)
        worker.scrape_finished.connect(finished.append)

        with qtbot.waitSignal(worker.scrape_finished, timeout=3000):
            worker.scrape_and_score("python")

        assert len(finished) == 1
        assert finished[0].keyword == "python"
        assert len(finished[0].scored) == 3
        assert "scraping" in logs
        assert "scoring" in logs
    finally:
        worker.stop_loop()


def test_scrape_cancel(qtbot, workdir, monkeypatch):
    async def fake(*, keyword, engine_workdir, location="Australia",
                   on_status=None, is_cancelled=None, max_jobs=12):
        on_status("scraping")
        for _ in range(300):
            if is_cancelled():
                raise asyncio.CancelledError()
            await asyncio.sleep(0.05)
        return _fake_result(keyword)

    monkeypatch.setattr(worker_module, "scrape_and_score", fake)

    worker = _build_worker(workdir)
    try:
        finished: list[ScrapeResult] = []
        worker.scrape_finished.connect(finished.append)

        with qtbot.waitSignal(worker.log, timeout=2000):
            worker.scrape_and_score("python")

        with qtbot.waitSignal(worker.scrape_finished, timeout=3000):
            worker.cancel()

        # The worker emits an empty ScrapeResult on cancellation with "cancelled" in errors.
        assert len(finished) == 1
        assert "cancelled" in (finished[0].errors or [])
    finally:
        worker.stop_loop()


def test_scrape_rejects_while_apply_running(qtbot, workdir, monkeypatch):
    async def slow_apply(**kwargs):
        await asyncio.sleep(30)
        from autoapply_next.engine.results import (
            ApplicationResult,
            ApplicationStatus,
        )
        return ApplicationResult(
            job_url=kwargs["job_url"], status=ApplicationStatus.DRY_RUN_VERIFIED
        )

    async def fake_scrape(**kwargs):
        return _fake_result(kwargs["keyword"])

    monkeypatch.setattr(worker_module, "apply_to_job", slow_apply)
    monkeypatch.setattr(worker_module, "scrape_and_score", fake_scrape)

    worker = _build_worker(workdir)
    try:
        failed: list[tuple[str, str]] = []
        worker.failed.connect(lambda op, msg: failed.append((op, msg)))

        worker.run_job("https://au.seek.com/job/1", False)
        qtbot.wait(100)
        with qtbot.waitSignal(worker.failed, timeout=2000):
            worker.scrape_and_score("python")

        assert any(op == "scrape" for op, _ in failed)
        worker.cancel()
    finally:
        worker.stop_loop()
