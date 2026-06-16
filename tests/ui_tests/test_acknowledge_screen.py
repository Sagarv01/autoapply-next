"""AcknowledgeScreen: the onboarding honesty line + opt-in checkbox.

The user must tick the honesty line (applications go out under their name) before
the bot unlocks. Acknowledgement is persisted (off the GUI thread) and the screen
emits `acknowledged`.
"""

from __future__ import annotations

import pytest

from autoapply_next.onboarding import state as ob
from autoapply_next.ui.acknowledge_screen import AcknowledgeScreen
from autoapply_next.ui.async_task import AsyncTaskRunner
from autoapply_next.ui.run_status import HONESTY_LINE


@pytest.fixture
def runner(qtbot):
    r = AsyncTaskRunner()
    yield r
    r.stop()


def _screen(qtbot, runner, wd):
    s = AcknowledgeScreen(engine_workdir=wd, runner=runner)
    qtbot.addWidget(s)
    return s


def test_shows_the_honesty_line(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    assert HONESTY_LINE in s.honesty_text()


def test_continue_blocked_until_checked(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    assert not s.can_continue()
    s.set_checked(True)
    assert s.can_continue()


def test_acknowledge_persists_and_emits(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    s.set_checked(True)
    with qtbot.waitSignal(s.acknowledged, timeout=3000):
        s.submit()
    assert ob.load_flags(tmp_path)["acknowledged"] is True


def test_submit_without_check_does_nothing(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    got: list = []
    s.acknowledged.connect(lambda: got.append(1))
    s.submit()  # not checked
    assert got == []
    assert ob.load_flags(tmp_path)["acknowledged"] is False
