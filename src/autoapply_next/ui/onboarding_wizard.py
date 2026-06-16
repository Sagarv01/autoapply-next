"""OnboardingWizard: sequences the onboarding screens and gates the bot.

A QStackedWidget over the five onboarding screens. On construction (and after each
screen reports its step done) it consults onboarding/state for the first incomplete
step and shows that screen; when nothing is left it emits `completed`. Routing is
driven by the same state model as the gate, so the UI can never show a step the
gate considers done (or skip one it considers pending).

MainWindow shows this wizard while `is_onboarding_complete` is False and swaps to
the bot UI on `completed`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QStackedWidget

from ..onboarding import state as ob
from ..onboarding.state import OnboardingStep
from .acknowledge_screen import AcknowledgeScreen
from .criteria_screen import CriteriaScreen
from .documents_screen import DocumentsScreen
from .profile_facts_screen import ProfileFactsScreen
from .signin_screen import SignInScreen

logger = logging.getLogger(__name__)


class OnboardingWizard(QStackedWidget):
    completed = Signal()

    def __init__(self, *, engine_workdir, auth_manager, runner):
        super().__init__()
        self._engine_workdir = Path(engine_workdir)
        self._auth = auth_manager

        self._signin = SignInScreen(auth_manager=auth_manager, runner=runner)
        self._profile = ProfileFactsScreen(engine_workdir=engine_workdir, runner=runner)
        self._documents = DocumentsScreen(engine_workdir=engine_workdir, runner=runner)
        self._criteria = CriteriaScreen(engine_workdir=engine_workdir, runner=runner)
        self._acknowledge = AcknowledgeScreen(engine_workdir=engine_workdir, runner=runner)

        self._screens = {
            OnboardingStep.ACCOUNT: self._signin,
            OnboardingStep.PROFILE: self._profile,
            OnboardingStep.DOCUMENTS: self._documents,
            OnboardingStep.CRITERIA: self._criteria,
            OnboardingStep.ACKNOWLEDGE: self._acknowledge,
        }
        for screen in self._screens.values():
            self.addWidget(screen)

        # Each screen reports its step done; advance to the next incomplete one.
        self._signin.authenticated.connect(self._advance)
        self._profile.saved.connect(self._advance)
        self._documents.documents_ready.connect(self._advance)
        self._criteria.saved.connect(self._advance)
        self._acknowledge.acknowledged.connect(self._advance)

        self._current: OnboardingStep | None = None
        self._advance()

    # -------------------------------------------------------- public/test API
    def current_step(self) -> OnboardingStep | None:
        return self._current

    def screen_for(self, step: OnboardingStep):
        return self._screens[step]

    # -------------------------------------------------------- routing
    def _advance(self) -> None:
        step = ob.first_incomplete_step(
            self._engine_workdir,
            signed_in=self._auth.signed_in,
            acknowledged=ob.load_flags(self._engine_workdir)["acknowledged"],
        )
        self._current = step
        if step is None:
            self.completed.emit()
            return
        self.setCurrentWidget(self._screens[step])
