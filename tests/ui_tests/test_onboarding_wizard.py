"""OnboardingWizard: sequences the five onboarding screens and gates the bot.

It shows the first incomplete step (account -> profile -> documents -> criteria
-> acknowledge), advances as each screen reports its step done, and emits
`completed` when the whole set is satisfied. Routing is driven by the same
onboarding/state model the gate uses, so UI and gate never disagree.
"""

from __future__ import annotations

import pytest

from autoapply_next.onboarding import state as ob
from autoapply_next.onboarding.profile_facts import save_facts
from autoapply_next.onboarding.state import OnboardingStep
from autoapply_next.ui.async_task import AsyncTaskRunner
from autoapply_next.ui.criteria_screen import save_criteria
from autoapply_next.ui.onboarding_wizard import OnboardingWizard
from tests.contract.test_profile_facts import _FULL


class _FakeAuth:
    def __init__(self, signed_in=False):
        self._si = signed_in

    @property
    def signed_in(self):
        return self._si

    def sign_in(self, email, password):
        self._si = True

    def sign_up(self, email, password, full_name=""):
        self._si = True


@pytest.fixture
def runner(qtbot):
    r = AsyncTaskRunner()
    yield r
    r.stop()


def _wizard(qtbot, runner, wd, auth):
    w = OnboardingWizard(engine_workdir=wd, auth_manager=auth, runner=runner)
    qtbot.addWidget(w)
    return w


def _complete_profile(wd):
    save_facts(wd, dict(_FULL))


def _complete_documents(wd):
    (wd / "assets").mkdir(parents=True, exist_ok=True)
    (wd / "assets" / ob.BASE_RESUME_FILENAME).write_bytes(b"resume")


def _complete_criteria(wd):
    save_criteria(wd, ["AWS"], "Sydney")


def test_routes_to_account_when_not_signed_in(qtbot, runner, tmp_path):
    w = _wizard(qtbot, runner, tmp_path, _FakeAuth(signed_in=False))
    assert w.current_step() == OnboardingStep.ACCOUNT


def test_routes_to_profile_when_signed_in_only(qtbot, runner, tmp_path):
    w = _wizard(qtbot, runner, tmp_path, _FakeAuth(signed_in=True))
    assert w.current_step() == OnboardingStep.PROFILE


def test_routes_to_documents_after_profile(qtbot, runner, tmp_path):
    _complete_profile(tmp_path)
    w = _wizard(qtbot, runner, tmp_path, _FakeAuth(signed_in=True))
    assert w.current_step() == OnboardingStep.DOCUMENTS


def test_routes_to_acknowledge_last(qtbot, runner, tmp_path):
    _complete_profile(tmp_path)
    _complete_documents(tmp_path)
    _complete_criteria(tmp_path)
    w = _wizard(qtbot, runner, tmp_path, _FakeAuth(signed_in=True))
    assert w.current_step() == OnboardingStep.ACKNOWLEDGE


def test_completing_last_step_emits_completed(qtbot, runner, tmp_path):
    _complete_profile(tmp_path)
    _complete_documents(tmp_path)
    _complete_criteria(tmp_path)
    w = _wizard(qtbot, runner, tmp_path, _FakeAuth(signed_in=True))
    ack = w.screen_for(OnboardingStep.ACKNOWLEDGE)
    ack.set_checked(True)
    with qtbot.waitSignal(w.completed, timeout=3000):
        ack.submit()
    assert w.current_step() is None  # nothing left


def test_all_complete_reports_no_current_step(qtbot, runner, tmp_path):
    _complete_profile(tmp_path)
    _complete_documents(tmp_path)
    _complete_criteria(tmp_path)
    ob.set_acknowledged(tmp_path, True)
    w = _wizard(qtbot, runner, tmp_path, _FakeAuth(signed_in=True))
    assert w.current_step() is None
