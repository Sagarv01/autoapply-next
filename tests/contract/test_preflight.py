"""TASKS 5.1: hard preflight checklist with 8 coded errors, each deep-linking to
the onboarding wizard step that fixes it. 5.2's engine-start gate (assert_ready)
raises PreflightBlocked carrying every red check; there is no bypass flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoapply_next.engine.preflight import (
    PreflightBlocked,
    PreflightCode,
    PreflightError,
    WIZARD_STEP,
    assert_ready,
    run_preflight,
)

_FULL_CANDIDATE = {
    "name": "Test User",
    "email": "t@example.com",
    "has_work_rights": True,
    "has_drivers_licence": True,
    "notice_period": "Immediately available",
    "salary_target_aud": 130000,
    "years_experience": "10+",
    "willing_to_relocate": False,
}

_GREEN = dict(
    entitlement_active=True,
    proxy_reachable=True,
    submissions_enabled=True,
    browser_available=True,
)


def _make_workdir(tmp_path: Path, *, candidate=None, search=None, resume=True, session=True) -> Path:
    import yaml

    cfg = {
        "candidate": _FULL_CANDIDATE if candidate is None else candidate,
        "search": {"skills": ["python", "aws"], "location": "Australia"} if search is None else search,
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (tmp_path / "assets").mkdir(exist_ok=True)
    if resume:
        (tmp_path / "assets" / "profile.txt").write_text("Test User\nSenior engineer.\n", encoding="utf-8")
    if session:
        (tmp_path / "sessions" / "seek").mkdir(parents=True, exist_ok=True)
        (tmp_path / "sessions" / "seek" / "state.json").write_text('{"cookies":[1]}', encoding="utf-8")
    return tmp_path


def _codes(errors: list[PreflightError]) -> set[PreflightCode]:
    return {e.code for e in errors}


# ----------------------------------------------------------- all green


def test_fully_ready_returns_no_errors(tmp_path):
    wd = _make_workdir(tmp_path)
    assert run_preflight(wd, **_GREEN) == []


def test_assert_ready_passes_when_green(tmp_path):
    wd = _make_workdir(tmp_path)
    assert_ready(wd, **_GREEN)  # must not raise


# ----------------------------------------------------- local checks


def test_missing_resume(tmp_path):
    wd = _make_workdir(tmp_path, resume=False)
    assert PreflightCode.MISSING_RESUME in _codes(run_preflight(wd, **_GREEN))


def test_incomplete_answer_bank_lists_missing(tmp_path):
    partial = {"name": "X", "has_work_rights": True}  # missing 5 of the 6 fields
    wd = _make_workdir(tmp_path, candidate=partial)
    errors = run_preflight(wd, **_GREEN)
    err = next(e for e in errors if e.code == PreflightCode.INCOMPLETE_ANSWER_BANK)
    assert set(err.detail["missing"]) == {
        "has_drivers_licence", "notice_period", "salary_target_aud",
        "years_experience", "willing_to_relocate",
    }


def test_complete_answer_bank_clears(tmp_path):
    wd = _make_workdir(tmp_path)
    assert PreflightCode.INCOMPLETE_ANSWER_BANK not in _codes(run_preflight(wd, **_GREEN))


def test_no_criteria(tmp_path):
    wd = _make_workdir(tmp_path, search={"skills": [], "location": ""})
    assert PreflightCode.NO_CRITERIA in _codes(run_preflight(wd, **_GREEN))


def test_seek_session_missing(tmp_path):
    wd = _make_workdir(tmp_path, session=False)
    assert PreflightCode.SEEK_SESSION_EXPIRED in _codes(run_preflight(wd, **_GREEN))


# ----------------------------------------------------- live checks


@pytest.mark.parametrize(
    "flag,code",
    [
        ("entitlement_active", PreflightCode.ENTITLEMENT_INACTIVE),
        ("proxy_reachable", PreflightCode.PROXY_UNREACHABLE),
        ("submissions_enabled", PreflightCode.KILL_SWITCH_ACTIVE),
        ("browser_available", PreflightCode.BROWSER_MISSING),
    ],
)
def test_live_check_fires(tmp_path, flag, code):
    wd = _make_workdir(tmp_path)
    live = {**_GREEN, flag: False}
    assert code in _codes(run_preflight(wd, **live))


# ----------------------------------------------------- coded errors + deep-links


def test_every_code_has_a_wizard_step_mapping():
    for code in PreflightCode:
        assert code in WIZARD_STEP  # KILL_SWITCH_ACTIVE maps to None (not user-fixable)


def test_user_fixable_codes_deep_link_to_a_step(tmp_path):
    # Everything red at once.
    wd = _make_workdir(tmp_path, candidate={"name": "x"}, search={}, resume=False, session=False)
    errors = run_preflight(wd, entitlement_active=False, proxy_reachable=False, submissions_enabled=False, browser_available=False)
    by_code = {e.code: e for e in errors}
    assert by_code[PreflightCode.MISSING_RESUME].wizard_step == "profile"
    assert by_code[PreflightCode.INCOMPLETE_ANSWER_BANK].wizard_step == "answers"
    assert by_code[PreflightCode.NO_CRITERIA].wizard_step == "criteria"
    assert by_code[PreflightCode.SEEK_SESSION_EXPIRED].wizard_step == "seek_connect"
    assert by_code[PreflightCode.ENTITLEMENT_INACTIVE].wizard_step == "account"
    assert by_code[PreflightCode.BROWSER_MISSING].wizard_step == "environment"
    assert by_code[PreflightCode.PROXY_UNREACHABLE].wizard_step == "environment"


# ----------------------------------------------------- the gate (5.2 primitive)


def test_assert_ready_raises_with_all_errors_when_red(tmp_path):
    wd = _make_workdir(tmp_path, resume=False)
    with pytest.raises(PreflightBlocked) as ei:
        assert_ready(wd, entitlement_active=False, proxy_reachable=True, submissions_enabled=True, browser_available=True)
    codes = {e.code for e in ei.value.errors}
    assert PreflightCode.MISSING_RESUME in codes
    assert PreflightCode.ENTITLEMENT_INACTIVE in codes


def test_assert_ready_has_no_bypass_parameter():
    # Invariant: there is no flag to skip the gate. assert_ready accepts only the
    # workdir + the live-check booleans; nothing named bypass/force/skip exists.
    import inspect

    params = set(inspect.signature(assert_ready).parameters)
    assert not (params & {"bypass", "force", "skip", "skip_preflight", "allow"})
