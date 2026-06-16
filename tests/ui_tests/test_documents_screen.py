"""DocumentsScreen: install the base resume (and optional cover) where the engine
reads them. The file copy runs off the GUI thread; the resume is required, the
cover is optional. (The QFileDialog picker itself is manual-verify.)
"""

from __future__ import annotations

import pytest

from autoapply_next.onboarding import state as ob
from autoapply_next.ui.async_task import AsyncTaskRunner
from autoapply_next.ui.documents_screen import DocumentsScreen


@pytest.fixture
def runner(qtbot):
    r = AsyncTaskRunner()
    yield r
    r.stop()


def _screen(qtbot, runner, wd):
    s = DocumentsScreen(engine_workdir=wd, runner=runner)
    qtbot.addWidget(s)
    return s


def test_no_resume_initially(qtbot, runner, tmp_path):
    wd = tmp_path / "engine"
    wd.mkdir()
    s = _screen(qtbot, runner, wd)
    assert not s.has_resume()


def test_install_resume_copies_to_engine_path_and_emits(qtbot, runner, tmp_path):
    src = tmp_path / "my_resume.docx"
    src.write_bytes(b"docx-bytes")
    wd = tmp_path / "engine"
    wd.mkdir()
    s = _screen(qtbot, runner, wd)
    with qtbot.waitSignal(s.documents_ready, timeout=3000):
        s.install_resume(str(src))
    dest = wd / "assets" / ob.BASE_RESUME_FILENAME
    assert dest.exists() and dest.read_bytes() == b"docx-bytes"
    assert s.has_resume()


def test_reports_existing_resume_on_open(qtbot, runner, tmp_path):
    wd = tmp_path / "engine"
    (wd / "assets").mkdir(parents=True)
    (wd / "assets" / ob.BASE_RESUME_FILENAME).write_bytes(b"x")
    s = _screen(qtbot, runner, wd)
    assert s.has_resume()


def test_install_cover_is_optional(qtbot, runner, tmp_path):
    src = tmp_path / "cover.docx"
    src.write_bytes(b"cover-bytes")
    wd = tmp_path / "engine"
    wd.mkdir()
    s = _screen(qtbot, runner, wd)
    with qtbot.waitSignal(runner.succeeded, timeout=3000):
        s.install_cover(str(src))
    assert (wd / "assets" / ob.BASE_COVER_FILENAME).read_bytes() == b"cover-bytes"
