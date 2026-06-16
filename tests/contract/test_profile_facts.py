"""M-A / Phase B: the onboarding profile-facts model (the approved 19 fields).

Stored in config.yaml's `candidate` block (where the frozen engine reads them).
Required-field validation gates onboarding completion; visa fields are required
only when the user is not a citizen/PR; the 4 EEO fields are optional and
default to "prefer_not_to_say".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from autoapply_next.onboarding import profile_facts as pf


def _write_config(tmp_path: Path, candidate: dict, extra: dict | None = None) -> Path:
    cfg = {"candidate": candidate}
    if extra:
        cfg.update(extra)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return tmp_path


_FULL = {
    "has_work_rights": True,
    "is_citizen": False,
    "visa_label": "485 Temporary Graduate Visa",
    "visa_expiry": "8 July 2028",
    "needs_sponsorship": False,
    "has_security_clearance": False,
    "years_experience": "5+",
    "salary_target_aud": 130000,
    "notice_period": "Immediately available",
    "willing_to_relocate": True,
    "location": "Sydney",
    "has_drivers_licence": True,
    "has_own_car": False,
    "work_arrangement": "any",
    "highest_education": "Master of IT",
    "employment_status": "between_jobs",
}


def test_field_spec_has_all_19():
    keys = {f.key for f in pf.CANDIDATE_FIELDS}
    # 15 core + 4 EEO
    for k in _FULL:
        assert k in keys
    for k in ("gender", "identifies_aboriginal", "has_disability", "is_veteran"):
        assert k in keys
    assert len(pf.CANDIDATE_FIELDS) == 20  # 16 core incl. visa(2) + 4 EEO


def test_complete_facts_have_no_missing(tmp_path):
    wd = _write_config(tmp_path, _FULL)
    facts = pf.load_facts(wd)
    assert pf.missing_required(facts) == []
    assert pf.is_complete(facts)


def test_missing_required_lists_unanswered(tmp_path):
    partial = {"has_work_rights": True, "is_citizen": True}
    wd = _write_config(tmp_path, partial)
    facts = pf.load_facts(wd)
    missing = pf.missing_required(facts)
    assert "salary_target_aud" in missing
    assert "notice_period" in missing
    assert not pf.is_complete(facts)


def test_visa_required_only_when_not_citizen(tmp_path):
    # Citizen/PR -> visa fields not required.
    citizen = {**_FULL, "is_citizen": True, "visa_label": "", "visa_expiry": ""}
    wd = _write_config(tmp_path, citizen)
    assert "visa_label" not in pf.missing_required(pf.load_facts(wd))

    # Not a citizen + blank visa -> visa fields required.
    noncitizen = {**_FULL, "is_citizen": False, "visa_label": "", "visa_expiry": ""}
    wd2 = _write_config(tmp_path / "b", noncitizen) if False else _write_config(tmp_path, noncitizen)
    miss = pf.missing_required(pf.load_facts(wd2))
    assert "visa_label" in miss and "visa_expiry" in miss


def test_eeo_optional_and_default_prefer_not_to_say(tmp_path):
    wd = _write_config(tmp_path, _FULL)  # no EEO keys set
    facts = pf.load_facts(wd)
    assert facts["gender"] == "prefer_not_to_say"
    assert facts["identifies_aboriginal"] == "prefer_not_to_say"
    # EEO never blocks completion
    for k in ("gender", "identifies_aboriginal", "has_disability", "is_veteran"):
        assert k not in pf.missing_required(facts)


def test_save_then_load_roundtrip_preserves_identity_and_other_keys(tmp_path):
    wd = _write_config(
        tmp_path,
        {"name": "Sagar", "email": "s@x.com", "phone": "+61400"},
        extra={"search": {"skills": ["AWS"], "location": "Australia"}},
    )
    pf.save_facts(wd, _FULL)
    raw = yaml.safe_load((wd / "config.yaml").read_text())
    cand = raw["candidate"]
    # facts written
    assert cand["salary_target_aud"] == 130000
    assert cand["is_citizen"] is False
    # identity preserved
    assert cand["name"] == "Sagar" and cand["email"] == "s@x.com"
    # other top-level blocks preserved
    assert raw["search"]["skills"] == ["AWS"]
    # reload sees them
    assert pf.is_complete(pf.load_facts(wd))


def test_save_coerces_salary_to_int(tmp_path):
    wd = _write_config(tmp_path, {})
    pf.save_facts(wd, {**_FULL, "salary_target_aud": "130000"})
    raw = yaml.safe_load((wd / "config.yaml").read_text())
    assert raw["candidate"]["salary_target_aud"] == 130000
    assert isinstance(raw["candidate"]["salary_target_aud"], int)
