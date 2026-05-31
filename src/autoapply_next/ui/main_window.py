"""MainWindow: holds the engine worker thread and routes between the 7 screens.

The worker is built ONCE here and shared across screens that need to run the
engine. This keeps the worker a singleton (per ADR-0003) and ensures the
safety gate's process-global patch is owned by exactly one place.

The window starts on RunScreen for the walking skeleton (Phase 2). Phase 3
swaps the start screen to SignInScreen.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QWidget,
)

from ..engine.worker import EngineWorker
from ..safe_ui import get_bus, safe_slot, show_error_dialog
from .batch_screen import BatchScreen
from .profile_screen import ProfileScreen
from .queue_screen import QueueScreen
from .results_screen import ResultsScreen
from .run_screen import RunScreen
from .session_setup_screen import SessionSetupScreen
from .settings_screen import SettingsScreen
from .signin_screen import SignInScreen
from .settings_store import SettingsStore

logger = logging.getLogger(__name__)


SCREEN_NAMES = [
    "Sign in",
    "Seek session",
    "Profile",
    "Queue",
    "Run",
    "Batch",
    "Results",
    "Settings",
]


class MainWindow(QMainWindow):
    def __init__(self, *, engine_workdir: Path):
        super().__init__()
        self.setWindowTitle("AutoApply Next")
        self.resize(1100, 760)

        self._engine_workdir = engine_workdir
        self._settings = SettingsStore(engine_workdir / "ui-settings.json")

        # Worker owns its own Python thread + asyncio loop internally.
        # No QThread / moveToThread needed.
        self._worker = EngineWorker(
            engine_workdir=engine_workdir,
            match_threshold=self._settings.match_threshold,
        )

        # Screens.
        self._stack = QStackedWidget(self)
        self._signin = SignInScreen()
        self._session = SessionSetupScreen(
            engine_workdir=engine_workdir, worker=self._worker
        )
        self._profile = ProfileScreen(engine_workdir=engine_workdir)
        self._queue = QueueScreen(
            engine_workdir=engine_workdir,
            worker=self._worker,
            settings=self._settings,
        )
        self._run = RunScreen(
            worker=self._worker,
            settings=self._settings,
        )
        self._batch = BatchScreen(
            engine_workdir=engine_workdir,
            worker=self._worker,
            settings=self._settings,
        )
        self._results = ResultsScreen(engine_workdir=engine_workdir)
        self._settings_screen = SettingsScreen(settings=self._settings)

        # Wire Queue -> Run handoff: selecting a row swaps to Run with URL preloaded.
        self._queue.run_requested.connect(self._on_queue_run_requested)
        # Sign-in is a stub; the Skip button emits `authenticated` and we
        # respond by routing to the Seek session screen, the natural next step.
        self._signin.authenticated.connect(self._on_signin_authenticated)
        for screen in [
            self._signin,
            self._session,
            self._profile,
            self._queue,
            self._run,
            self._batch,
            self._results,
            self._settings_screen,
        ]:
            self._stack.addWidget(screen)
        self.setCentralWidget(self._stack)

        self._build_toolbar()
        self._build_status_bar()
        self._wire_signals()

        # Walking-skeleton default: start on Run screen.
        # Phase 3 changes this to SignInScreen on first launch.
        self._stack.setCurrentWidget(self._run)
        self._highlight_action(self._run)

        # Wire ALLOW_REAL_SUBMIT changes to update the worker / status bar.
        self._settings.allow_real_submit_changed.connect(
            self._on_allow_real_submit_changed
        )
        self._on_allow_real_submit_changed(self._settings.allow_real_submit)

    # -------------------------------------------------------------- layout

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Navigation", self)
        toolbar.setMovable(False)
        toolbar.setObjectName("nav-toolbar")
        self.addToolBar(Qt.LeftToolBarArea, toolbar)
        self._actions: dict[QWidget, QAction] = {}
        screens = [
            (self._signin, "Sign in"),
            (self._session, "Seek session"),
            (self._profile, "Profile"),
            (self._queue, "Queue"),
            (self._run, "Run"),
            (self._batch, "Batch"),
            (self._results, "Results"),
            (self._settings_screen, "Settings"),
        ]
        for i, (screen, label) in enumerate(screens):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setShortcut(QKeySequence(f"Ctrl+{i+1}"))
            action.triggered.connect(lambda _checked, s=screen: self._goto(s))
            toolbar.addAction(action)
            self._actions[screen] = action

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        self.setStatusBar(bar)
        self._gate_label = QLabel("DRY-RUN", self)
        self._gate_label.setStyleSheet(
            "padding: 2px 8px; background: #15803d; color: white; "
            "border-radius: 4px; font-weight: bold;"
        )
        bar.addPermanentWidget(self._gate_label)
        self._engine_label = QLabel(f"engine: {self._engine_workdir}", self)
        self._engine_label.setStyleSheet("color: #6b7280;")
        bar.addWidget(self._engine_label)

    def _wire_signals(self) -> None:
        self._worker.state_changed.connect(self._on_worker_state)
        self._worker.failed.connect(self._on_worker_failed)
        self._settings.match_threshold_changed.connect(self._on_threshold_changed)
        # Surface any uncaught exception / Qt critical / safe_slot capture.
        get_bus().error.connect(self._on_bus_error)

    # -------------------------------------------------------------- navigation

    def _goto(self, screen: QWidget) -> None:
        self._stack.setCurrentWidget(screen)
        self._highlight_action(screen)

    def _highlight_action(self, screen: QWidget) -> None:
        for s, action in self._actions.items():
            action.setChecked(s is screen)

    # -------------------------------------------------------------- slots

    @Slot(bool)
    def _on_allow_real_submit_changed(self, allowed: bool) -> None:
        if allowed:
            self._gate_label.setText("LIVE SUBMIT")
            self._gate_label.setStyleSheet(
                "padding: 2px 8px; background: #b91c1c; color: white; "
                "border-radius: 4px; font-weight: bold;"
            )
        else:
            self._gate_label.setText("DRY-RUN")
            self._gate_label.setStyleSheet(
                "padding: 2px 8px; background: #15803d; color: white; "
                "border-radius: 4px; font-weight: bold;"
            )

    @Slot(str)
    def _on_worker_state(self, state: str) -> None:
        self.statusBar().showMessage(f"Engine: {state}", 5000)

    @Slot(str, str)
    @safe_slot
    def _on_worker_failed(self, op: str, message: str) -> None:
        show_error_dialog(
            self,
            f"Engine error: {op}",
            (
                f"{message}\n\nThe app is still usable. Try the operation again, "
                "or use the toolbar to switch screens."
            ),
        )

    @Slot(str, str)
    @safe_slot
    def _on_bus_error(self, title: str, detail: str) -> None:
        # Detail can be long; show a short summary up top, full text in expander.
        summary = detail.splitlines()[0] if detail else "An error occurred."
        show_error_dialog(self, title or "Error", summary, detail)

    @Slot(str)
    @safe_slot
    def _on_queue_run_requested(self, url: str) -> None:
        self._run.set_url(url)
        self._goto(self._run)

    @Slot(str)
    @safe_slot
    def _on_signin_authenticated(self, _user_id: str) -> None:
        # Stub sign-in done; nudge to the natural next step.
        self.statusBar().showMessage("Signed in (dev mode). Set up your Seek session next.", 5000)
        self._goto(self._session)

    # Also bump the worker's threshold when Settings changes.
    @Slot(int)
    @safe_slot
    def _on_threshold_changed(self, value: int) -> None:
        self._worker.set_match_threshold(value)

    # -------------------------------------------------------------- shutdown

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._worker.stop_loop()
        super().closeEvent(event)
