from __future__ import annotations

from autoapply_next.audience import Audience, current_audience
from autoapply_next.ui.main_window import MainWindow
from autoapply_next.ui.settings_screen import SettingsScreen
from autoapply_next.ui.settings_store import SettingsStore


def test_source_checkout_defaults_to_tester(monkeypatch):
    monkeypatch.delenv("AUTOAPPLY_BUILD_AUDIENCE", raising=False)
    monkeypatch.delenv("AUTOAPPLY_AUDIENCE", raising=False)
    assert current_audience() is Audience.TESTER


def test_user_build_hides_tester_only_screens(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOAPPLY_BUILD_AUDIENCE", "user")
    win = MainWindow(engine_workdir=tmp_path)
    qtbot.addWidget(win)

    assert not win._actions[win._profile].isVisible()
    assert not win._actions[win._profile].isEnabled()
    assert not win._actions[win._run].isVisible()
    assert not win._actions[win._run].isEnabled()
    assert win._actions[win._queue].isVisible()
    assert win._actions[win._settings_screen].isVisible()

    current = win._stack.currentWidget()
    win._goto(win._run)
    assert win._stack.currentWidget() is current
    assert win._queue._table.isColumnHidden(5)
    assert win.windowTitle() == "AutoApply"
    assert win._gate_label is None
    assert win._engine_label.text() == "AutoApply is ready"
    assert win._batch._mode_label.isHidden()
    assert win._batch._threshold_label.isHidden()
    assert win._batch._stop_btn.text() == "Stop"
    win.close()


def test_user_settings_hide_throttle_and_force_pacing(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOAPPLY_BUILD_AUDIENCE", "user")
    settings = SettingsStore(tmp_path / "ui-settings.json")
    settings.pace_between_applies = False

    screen = SettingsScreen(settings=settings)
    qtbot.addWidget(screen)

    assert settings.pace_between_applies is True
    assert screen._pace_box.isHidden()
    assert screen._pace_label.isHidden()
    assert not hasattr(screen, "_allow_box")
