"""The 19-question screening profile form (Phase B), dynamic from CANDIDATE_FIELDS.

Renders an input per fact, defaults EEO to prefer_not_to_say, shows the visa
fields only when the user is not a citizen/PR, validates required fields against
the same gate the wizard uses, and SAVES off the GUI thread via the runner.
"""

from __future__ import annotations

import pytest

from autoapply_next.onboarding import profile_facts
from autoapply_next.onboarding.profile_facts import CANDIDATE_FIELDS
from autoapply_next.ui.async_task import AsyncTaskRunner
from autoapply_next.ui.profile_facts_screen import ProfileFactsScreen


@pytest.fixture
def runner(qtbot):
    r = AsyncTaskRunner()
    yield r
    r.stop()


def _screen(qtbot, runner, wd):
    s = ProfileFactsScreen(engine_workdir=wd, runner=runner)
    qtbot.addWidget(s)
    return s


def _fill_complete(s):
    s.set_field("has_work_rights", True)
    s.set_field("is_citizen", False)
    s.set_field("visa_label", "485 Temporary Graduate Visa")
    s.set_field("visa_expiry", "2027-01-01")
    s.set_field("needs_sponsorship", False)
    s.set_field("has_security_clearance", False)
    s.set_field("years_experience", "5+")
    s.set_field("salary_target_aud", "130000")
    s.set_field("notice_period", "Immediately available")
    s.set_field("willing_to_relocate", False)
    s.set_field("location", "Sydney")
    s.set_field("has_drivers_licence", True)
    s.set_field("has_own_car", True)
    s.set_field("work_arrangement", "any")
    s.set_field("highest_education", "Master of IT")
    s.set_field("employment_status", "between_jobs")


def test_renders_an_input_for_every_field(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    for f in CANDIDATE_FIELDS:
        assert s.has_field(f.key), f.key


def test_eeo_fields_default_to_prefer_not_to_say(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    vals = s.collect_values()
    for k in ("gender", "identifies_aboriginal", "has_disability", "is_veteran"):
        assert vals[k] == "prefer_not_to_say"


def test_visa_fields_are_conditional_on_citizenship(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    s.set_field("is_citizen", True)
    assert not s.is_field_visible("visa_label")
    assert not s.is_field_visible("visa_expiry")
    s.set_field("is_citizen", False)
    assert s.is_field_visible("visa_label")
    assert s.is_field_visible("visa_expiry")


def test_missing_required_blocks_save(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    s.submit_save()  # nothing filled
    assert s.error_text()
    assert not (tmp_path / "config.yaml").exists()  # nothing persisted


def test_complete_form_saves_off_thread_and_emits(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    _fill_complete(s)
    with qtbot.waitSignal(s.saved, timeout=3000):
        s.submit_save()
    vals = profile_facts.load_facts(tmp_path)
    assert profile_facts.is_complete(vals)
    assert vals["location"] == "Sydney"
    assert vals["is_citizen"] is False
    assert vals["salary_target_aud"] == 130000  # int coerced on save


def test_citizen_does_not_require_visa(qtbot, runner, tmp_path):
    s = _screen(qtbot, runner, tmp_path)
    _fill_complete(s)
    s.set_field("is_citizen", True)
    s.set_field("visa_label", "")
    s.set_field("visa_expiry", "")
    with qtbot.waitSignal(s.saved, timeout=3000):
        s.submit_save()
    assert profile_facts.is_complete(profile_facts.load_facts(tmp_path))


def test_loads_existing_values(qtbot, runner, tmp_path):
    _fill_into_disk = ProfileFactsScreen(engine_workdir=tmp_path, runner=runner)
    # save a complete set, then a fresh screen should reflect it
    _fill_complete(_fill_into_disk)
    profile_facts.save_facts(tmp_path, _fill_into_disk.collect_values())
    s = _screen(qtbot, runner, tmp_path)
    assert s.collect_values()["location"] == "Sydney"
