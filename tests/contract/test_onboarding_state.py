"""M-A: onboarding completion gates the bot.

The real bot UI stays locked until every step is complete: signed in, profile
facts complete, base resume present, job criteria set, and the honesty line
acknowledged. Completion is persisted so onboarding never re-runs.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from autoapply_next.onboarding import state as ob
from autoapply_next.onboarding.state import OnboardingStep
from tests.contract.test_profile_facts import _FULL  # reuse the complete fact set


def _ready_workdir(tmp_path: Path) -> Path:
    cfg = {
        "candidate": {"name": "Sagar", "email": "s@x.com", "phone": "+61400", **_FULL},
        "search": {"skills": ["AWS"], "location": "Sydney", "match_threshold": 45},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / ob.BASE_RESUME_FILENAME).write_bytes(b"docx-bytes")
    return tmp_path


def test_all_steps_complete_unlocks(tmp_path):
    wd = _ready_workdir(tmp_path)
    assert ob.is_onboarding_complete(wd, signed_in=True, acknowledged=True)
    assert ob.first_incomplete_step(wd, signed_in=True, acknowledged=True) is None


def test_account_gates_when_not_signed_in(tmp_path):
    wd = _ready_workdir(tmp_path)
    assert not ob.is_onboarding_complete(wd, signed_in=False, acknowledged=True)
    assert ob.first_incomplete_step(wd, signed_in=False, acknowledged=True) == OnboardingStep.ACCOUNT


def test_documents_gate_without_resume(tmp_path):
    wd = _ready_workdir(tmp_path)
    (wd / "assets" / ob.BASE_RESUME_FILENAME).unlink()
    assert ob.first_incomplete_step(wd, signed_in=True, acknowledged=True) == OnboardingStep.DOCUMENTS


def test_criteria_gate_without_search(tmp_path):
    wd = _ready_workdir(tmp_path)
    cfg = yaml.safe_load((wd / "config.yaml").read_text())
    cfg["search"] = {"skills": [], "location": ""}
    (wd / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert ob.first_incomplete_step(wd, signed_in=True, acknowledged=True) == OnboardingStep.CRITERIA


def test_profile_gate_with_incomplete_facts(tmp_path):
    wd = _ready_workdir(tmp_path)
    cfg = yaml.safe_load((wd / "config.yaml").read_text())
    del cfg["candidate"]["salary_target_aud"]
    (wd / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert ob.first_incomplete_step(wd, signed_in=True, acknowledged=True) == OnboardingStep.PROFILE


def test_acknowledge_gates(tmp_path):
    wd = _ready_workdir(tmp_path)
    assert ob.first_incomplete_step(wd, signed_in=True, acknowledged=False) == OnboardingStep.ACKNOWLEDGE


def test_flags_persist_across_loads(tmp_path):
    wd = _ready_workdir(tmp_path)
    assert ob.load_flags(wd) == {"acknowledged": False, "completed": False}
    ob.set_acknowledged(wd, True)
    ob.set_completed(wd, True)
    flags = ob.load_flags(wd)
    assert flags["acknowledged"] is True and flags["completed"] is True
    # a fresh read still sees them (persisted to disk)
    assert ob.load_flags(wd)["completed"] is True
