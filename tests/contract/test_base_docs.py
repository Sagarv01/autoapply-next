"""Free/Basic apply with base docs (no LLM tailoring); Pro tailors per-job.

The apply flow's document step branches on tailoring_policy: Pro -> tailorer.tailor
(LLM), non-Pro -> base resume + base cover converted to PDF as-is. The real
LibreOffice conversion is manual-verify; here the vendored machinery is mocked.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from autoapply_next.engine import adapter
from autoapply_next.engine import tailoring_policy as tp


class _Job:
    company = "Acme"
    title = "Engineer"
    url = "https://www.seek.com.au/job/1"


class _FakeLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _reset():
    tp.set_tailoring_allowed(True)
    yield
    tp.set_tailoring_allowed(True)


async def test_produce_documents_tailors_when_allowed():
    tp.set_tailoring_allowed(True)
    seen = {}

    async def tailor(job, tier="full"):
        seen["tier"] = tier
        return ("tailored_r.pdf", "tailored_c.pdf")

    out = await adapter._produce_documents(_Job(), types.SimpleNamespace(tailor=tailor))
    assert out == ("tailored_r.pdf", "tailored_c.pdf")
    assert seen["tier"] == "full"


async def test_produce_documents_uses_base_when_not_allowed(monkeypatch):
    tp.set_tailoring_allowed(False)

    async def tailor(*a, **k):
        raise AssertionError("must NOT tailor for a non-Pro user")

    async def fake_base(job, tailorer):
        return ("base_r.pdf", "base_c.pdf")

    monkeypatch.setattr(adapter, "_export_base_documents", fake_base)
    out = await adapter._produce_documents(_Job(), types.SimpleNamespace(tailor=tailor))
    assert out == ("base_r.pdf", "base_c.pdf")


async def test_export_base_documents_uses_vendored_resume_export(tmp_path):
    async def base_resume(job):
        return "base_resume.pdf"

    fake = types.SimpleNamespace(
        _export_base_resume_pdf=base_resume,
        ASSETS_DIR=tmp_path,
        detect_libreoffice=lambda: "/soffice",
        _libreoffice_path="/already-set",
    )
    # no base cover present -> cover is ""
    out = await adapter._export_base_documents(_Job(), fake)
    assert out == ("base_resume.pdf", "")


async def test_export_base_documents_detects_libreoffice_when_unset(tmp_path):
    # The base path bypasses tailorer.tailor()'s own detection; without detecting
    # here, _run_libreoffice gets a None binary and the apply fails at tailor.
    calls = {"detect": 0}

    async def base_resume(job):
        return "r.pdf"

    def detect():
        calls["detect"] += 1
        return "/Applications/LibreOffice.app/Contents/MacOS/soffice"

    fake = types.SimpleNamespace(
        _export_base_resume_pdf=base_resume,
        ASSETS_DIR=tmp_path,  # no cover
        detect_libreoffice=detect,
        _libreoffice_path=None,
    )
    await adapter._export_base_documents(_Job(), fake)
    assert calls["detect"] == 1
    assert fake._libreoffice_path == "/Applications/LibreOffice.app/Contents/MacOS/soffice"


async def test_export_base_documents_skips_detect_if_already_set(tmp_path):
    calls = {"detect": 0}

    async def base_resume(job):
        return "r.pdf"

    fake = types.SimpleNamespace(
        _export_base_resume_pdf=base_resume,
        ASSETS_DIR=tmp_path,
        detect_libreoffice=lambda: calls.__setitem__("detect", calls["detect"] + 1) or "/x",
        _libreoffice_path="/already/soffice",
    )
    await adapter._export_base_documents(_Job(), fake)
    assert calls["detect"] == 0  # already detected, don't re-run


async def test_export_base_cover_absent_returns_empty(tmp_path):
    fake = types.SimpleNamespace(ASSETS_DIR=tmp_path)
    assert await adapter._export_base_cover_pdf(_Job(), fake) == ""


async def test_export_base_cover_converts_when_present(tmp_path):
    (tmp_path / "base_cover_letter.docx").write_bytes(b"docx-bytes")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def make_filename(prefix, company, title):
        return f"{prefix}_{company}_{title}.pdf"

    async def run_lo(src, dst):
        # simulate LibreOffice producing the PDF
        (out_dir / "CoverLetter_Acme_Engineer.pdf").write_bytes(b"pdf-bytes")

    fake = types.SimpleNamespace(
        ASSETS_DIR=tmp_path,
        OUTPUT_DIR=out_dir,
        make_filename=make_filename,
        _pdf_lock=_FakeLock(),
        _run_libreoffice=run_lo,
    )
    out = await adapter._export_base_cover_pdf(_Job(), fake)
    assert out.endswith("CoverLetter_Acme_Engineer.pdf")
    assert Path(out).exists()
