"""Onboarding completion + gating: the bot UI stays locked until every step is
done, and completion is persisted so the wizard never re-runs for a set-up user.

Steps (in order): ACCOUNT (signed in) -> PROFILE (the 19 facts complete) ->
DOCUMENTS (base resume present where the engine reads it) -> CRITERIA (search
keywords + location set) -> ACKNOWLEDGE (the honesty line). `is_onboarding_complete`
is the gate the main window consults; `first_incomplete_step` drives the wizard.
"""
from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path

from . import profile_facts

logger = logging.getLogger(__name__)

# The frozen engine reads the base resume from this hardcoded filename
# (vendor/job-finder/tailorer.py:272,591). Onboarding saves the uploaded resume
# here so the bot actually uses it. (Engine wart; documented in HANDOFF.)
BASE_RESUME_FILENAME = "SAGAR VERMA.docx"
BASE_COVER_FILENAME = "base_cover_letter.docx"

_FLAGS_FILE = "onboarding.json"


class OnboardingStep(str, Enum):
    ACCOUNT = "account"
    PROFILE = "profile"
    DOCUMENTS = "documents"
    CRITERIA = "criteria"
    ACKNOWLEDGE = "acknowledge"


STEP_ORDER: tuple[OnboardingStep, ...] = (
    OnboardingStep.ACCOUNT,
    OnboardingStep.PROFILE,
    OnboardingStep.DOCUMENTS,
    OnboardingStep.CRITERIA,
    OnboardingStep.ACKNOWLEDGE,
)


def _criteria_complete(workdir) -> bool:
    search = profile_facts._read_config(workdir).get("search")
    search = search if isinstance(search, dict) else {}
    return bool(search.get("skills")) and bool(search.get("location"))


def _documents_complete(workdir) -> bool:
    return (Path(workdir) / "assets" / BASE_RESUME_FILENAME).exists()


def step_complete(workdir, step: OnboardingStep, *, signed_in: bool, acknowledged: bool) -> bool:
    if step == OnboardingStep.ACCOUNT:
        return bool(signed_in)
    if step == OnboardingStep.PROFILE:
        return profile_facts.is_complete(profile_facts.load_facts(workdir))
    if step == OnboardingStep.DOCUMENTS:
        return _documents_complete(workdir)
    if step == OnboardingStep.CRITERIA:
        return _criteria_complete(workdir)
    if step == OnboardingStep.ACKNOWLEDGE:
        return bool(acknowledged)
    return False


def steps_status(workdir, *, signed_in: bool, acknowledged: bool) -> dict[OnboardingStep, bool]:
    return {
        s: step_complete(workdir, s, signed_in=signed_in, acknowledged=acknowledged)
        for s in STEP_ORDER
    }


def first_incomplete_step(workdir, *, signed_in: bool, acknowledged: bool) -> OnboardingStep | None:
    for s in STEP_ORDER:
        if not step_complete(workdir, s, signed_in=signed_in, acknowledged=acknowledged):
            return s
    return None


def is_onboarding_complete(workdir, *, signed_in: bool, acknowledged: bool) -> bool:
    return first_incomplete_step(workdir, signed_in=signed_in, acknowledged=acknowledged) is None


# ----------------------------------------------------- persisted flags
def _flags_path(workdir) -> Path:
    return Path(workdir) / _FLAGS_FILE


def load_flags(workdir) -> dict:
    p = _flags_path(workdir)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("onboarding flags read failed: %s", exc)
    return {"acknowledged": bool(data.get("acknowledged")), "completed": bool(data.get("completed"))}


def _write_flags(workdir, flags: dict) -> None:
    _flags_path(workdir).write_text(json.dumps(flags), encoding="utf-8")


def set_acknowledged(workdir, value: bool = True) -> None:
    flags = load_flags(workdir)
    flags["acknowledged"] = bool(value)
    _write_flags(workdir, flags)


def set_completed(workdir, value: bool = True) -> None:
    flags = load_flags(workdir)
    flags["completed"] = bool(value)
    _write_flags(workdir, flags)
