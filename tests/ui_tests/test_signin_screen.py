"""Real Supabase sign-in screen, background auth via AsyncTaskRunner.

Email + password run AuthManager.sign_in OFF the GUI thread (it is a blocking
httpx call); on success the screen emits `authenticated(user_id)`; on an auth
error it shows a message and stays usable. Drives the real runner thread with a
synthetic auth manager (the design's recommended test approach).
"""

from __future__ import annotations

import pytest

from autoapply_next.auth.supabase_auth import AuthError, Session
from autoapply_next.ui.async_task import AsyncTaskRunner
from autoapply_next.ui.signin_screen import SignInScreen


def _session(uid="u-1"):
    return Session(access_token="a", refresh_token="r", user_id=uid, email="e@x.com", expires_at=9_999_999_999)


class _FakeAuth:
    def __init__(self, *, signin=None, signin_error=None, signup="__same__", signup_error=None):
        self._signin = signin
        self._signin_error = signin_error
        self._signup = signup
        self._signup_error = signup_error
        self.calls: list = []

    def sign_in(self, email, password):
        self.calls.append(("sign_in", email, password))
        if self._signin_error:
            raise self._signin_error
        return self._signin

    def sign_up(self, email, password, full_name=""):
        self.calls.append(("sign_up", email, password))
        if self._signup_error:
            raise self._signup_error
        return self._signin if self._signup == "__same__" else self._signup


@pytest.fixture
def runner(qtbot):
    r = AsyncTaskRunner()
    yield r
    r.stop()


def _screen(qtbot, auth, runner):
    s = SignInScreen(auth_manager=auth, runner=runner)
    qtbot.addWidget(s)
    return s


def test_successful_signin_emits_authenticated(qtbot, runner):
    auth = _FakeAuth(signin=_session("u-42"))
    screen = _screen(qtbot, auth, runner)
    screen.set_credentials("e@x.com", "pw")
    got: list = []
    screen.authenticated.connect(got.append)
    with qtbot.waitSignal(screen.authenticated, timeout=3000):
        screen.submit_sign_in()
    assert got == ["u-42"]
    assert auth.calls == [("sign_in", "e@x.com", "pw")]


def test_signin_error_shows_message_and_does_not_authenticate(qtbot, runner):
    auth = _FakeAuth(signin_error=AuthError("Invalid login credentials", status=400))
    screen = _screen(qtbot, auth, runner)
    screen.set_credentials("e@x.com", "wrong")
    got: list = []
    screen.authenticated.connect(got.append)
    with qtbot.waitSignal(runner.failed, timeout=3000):
        screen.submit_sign_in()
    qtbot.waitUntil(lambda: bool(screen.error_text()), timeout=2000)
    assert "Invalid login credentials" in screen.error_text()
    assert got == []
    assert screen.is_interactive()  # stays usable so the user can retry


def test_empty_fields_are_validated_without_calling_auth(qtbot, runner):
    auth = _FakeAuth(signin=_session())
    screen = _screen(qtbot, auth, runner)
    screen.set_credentials("", "")
    screen.submit_sign_in()
    assert "email" in screen.error_text().lower()
    assert auth.calls == []  # never submitted to the network


def test_sign_up_needing_email_confirmation_shows_message(qtbot, runner):
    # sign_up returns None when Supabase requires email confirmation.
    auth = _FakeAuth(signup=None)
    screen = _screen(qtbot, auth, runner)
    screen.set_credentials("new@x.com", "pw123456")
    got: list = []
    screen.authenticated.connect(got.append)
    with qtbot.waitSignal(runner.succeeded, timeout=3000):
        screen.submit_sign_up()
    qtbot.waitUntil(lambda: bool(screen.error_text()), timeout=2000)
    assert "email" in screen.error_text().lower()
    assert got == []  # not signed in yet; must confirm first


def test_controls_are_enabled_by_default(qtbot, runner):
    screen = _screen(qtbot, _FakeAuth(signin=_session()), runner)
    assert screen.is_interactive()  # real sign-in, not a disabled stub
