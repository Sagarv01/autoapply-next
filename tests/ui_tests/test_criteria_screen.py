"""CriteriaScreen: the jobs the bot searches for (skills + location).

Writes config.yaml's search block (skills list + location), preserving other
config (match_threshold, candidate). Saves off the GUI thread; emits `saved`.
"""

from __future__ import annotations

import pytest
import yaml

from autoapply_next.ui.async_task import AsyncTaskRunner
from autoapply_next.ui.criteria_screen import CriteriaScreen


@pytest.fixture
def runner(qtbot):
    r = AsyncTaskRunner()
    yield r
    r.stop()


def _screen(qtbot, runner, wd):
    s = CriteriaScreen(engine_workdir=wd, runner=runner)
    qtbot.addWidget(s)
    return s


def test_save_blocked_when_empty(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    s.set_criteria("", "")
    s.submit_save()
    assert s.error_text()
    assert not (tmp_path / "config.yaml").exists()


def test_saves_skills_and_location(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    s.set_criteria("AWS, DevOps, Cloud", "Sydney")
    with qtbot.waitSignal(s.saved, timeout=3000):
        s.submit_save()
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["search"]["skills"] == ["AWS", "DevOps", "Cloud"]
    assert cfg["search"]["location"] == "Sydney"


def test_loads_existing(qtbot, runner, tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"search": {"skills": ["X", "Y"], "location": "Perth"}})
    )
    s = _screen(qtbot, runner, tmp_path)
    assert "X" in s.skills_text() and "Y" in s.skills_text()
    assert s.location_text() == "Perth"


def test_preserves_other_config(qtbot, runner, tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"search": {"match_threshold": 45}, "candidate": {"name": "Sagar"}})
    )
    s = _screen(qtbot, runner, tmp_path)
    s.set_criteria("AWS", "Sydney")
    with qtbot.waitSignal(s.saved, timeout=3000):
        s.submit_save()
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["search"]["match_threshold"] == 45  # preserved
    assert cfg["candidate"]["name"] == "Sagar"      # preserved
