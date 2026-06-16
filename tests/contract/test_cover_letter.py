"""Cover letter is optional: a base-docs user with no cover applies resume-only."""

from __future__ import annotations

import sys
import types

from autoapply_next.engine.cover_letter import OptionalCoverLetter


def _fake_seek_apply(calls):
    m = types.ModuleType("seek_apply")

    async def _upload_cover_letter(page, cover_path, cover_name):
        calls.append((cover_path, cover_name))

    m._upload_cover_letter = _upload_cover_letter  # type: ignore[attr-defined]
    return m


async def test_skips_upload_when_no_cover_name(monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(calls))
    with OptionalCoverLetter():
        await sys.modules["seek_apply"]._upload_cover_letter("page", "/some/dir", "")
    assert calls == []  # skipped, no crash


async def test_skips_when_path_is_a_directory(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(calls))
    with OptionalCoverLetter():
        # empty cover_pdf resolves to the cwd directory
        await sys.modules["seek_apply"]._upload_cover_letter("page", str(tmp_path), "")
    assert calls == []


async def test_uploads_when_a_real_cover_is_present(monkeypatch, tmp_path):
    cover = tmp_path / "cover.pdf"
    cover.write_bytes(b"pdf")
    calls: list = []
    monkeypatch.setitem(sys.modules, "seek_apply", _fake_seek_apply(calls))
    with OptionalCoverLetter():
        await sys.modules["seek_apply"]._upload_cover_letter("page", str(cover), "cover.pdf")
    assert calls == [(str(cover), "cover.pdf")]


def test_install_restores(monkeypatch):
    fake = _fake_seek_apply([])
    monkeypatch.setitem(sys.modules, "seek_apply", fake)
    original = fake._upload_cover_letter
    c = OptionalCoverLetter()
    c.install()
    assert fake._upload_cover_letter is not original
    c.uninstall()
    assert fake._upload_cover_letter is original
