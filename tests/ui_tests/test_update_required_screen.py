"""UpdateRequiredScreen: the wall shown when the app is below the proxy's floor."""

from __future__ import annotations

from autoapply_next.version_check import DOWNLOAD_URL
from autoapply_next.ui.update_required_screen import UpdateRequiredScreen


def test_download_button_opens_the_download_url(qtbot):
    opened: list = []
    s = UpdateRequiredScreen(open_url=opened.append)
    qtbot.addWidget(s)
    s.download()
    assert opened == [DOWNLOAD_URL]


def test_shows_a_clear_message(qtbot):
    s = UpdateRequiredScreen(open_url=lambda _u: None)
    qtbot.addWidget(s)
    assert s.message_text().strip()
