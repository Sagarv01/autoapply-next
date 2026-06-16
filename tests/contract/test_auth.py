"""M-A / Phase 3: Supabase auth foundation.

- supabase_auth: GoTrue REST client (sign in/up/refresh/reset) -> Session.
- token_store: refresh token persisted in the OS keychain (keyring) only.
- AuthManager: holds the session, wires the engine's llm_proxy access-token
  provider + refresher, restores silently from the keychain, signs out cleanly.
No real network: _http_post and keyring are stubbed.
"""

from __future__ import annotations

import pytest

from autoapply_next.auth import manager as mgr_mod
from autoapply_next.auth import supabase_auth as sa
from autoapply_next.auth import token_store as ts
from autoapply_next.auth.manager import AuthManager
from autoapply_next.auth.supabase_auth import AuthError, Session
from autoapply_next.engine import llm_proxy


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._b = body

    def json(self):
        return self._b


def _token_payload(access="acc", refresh="ref", uid="u1", email="t@x.com", expires_in=3600):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
        "expires_at": 9999999999,
        "user": {"id": uid, "email": email},
    }


@pytest.fixture(autouse=True)
def _reset_proxy_hooks():
    yield
    llm_proxy.set_access_token_provider(None)
    llm_proxy.set_token_refresher(None)


# ---------------------------------------------------------- supabase_auth


def test_sign_in_returns_session(monkeypatch):
    captured = {}

    def _fake_post(path, body, **kw):
        captured["path"] = path
        captured["body"] = body
        return _Resp(200, _token_payload(access="A1", refresh="R1", uid="user-1", email="me@x.com"))

    monkeypatch.setattr(sa, "_http_post", _fake_post)
    s = sa.sign_in("me@x.com", "pw")
    assert isinstance(s, Session)
    assert (s.access_token, s.refresh_token, s.user_id, s.email) == ("A1", "R1", "user-1", "me@x.com")
    assert "grant_type=password" in captured["path"]
    assert captured["body"] == {"email": "me@x.com", "password": "pw"}


def test_sign_in_bad_credentials_raises(monkeypatch):
    monkeypatch.setattr(sa, "_http_post", lambda p, b, **k: _Resp(400, {"error_description": "Invalid login credentials"}))
    with pytest.raises(AuthError):
        sa.sign_in("me@x.com", "wrong")


def test_sign_up_returns_session_when_no_confirmation(monkeypatch):
    monkeypatch.setattr(sa, "_http_post", lambda p, b, **k: _Resp(200, _token_payload()))
    s = sa.sign_up("new@x.com", "pw", "New User")
    assert isinstance(s, Session)


def test_sign_up_returns_none_when_confirmation_required(monkeypatch):
    # GoTrue returns a user object with no access_token when email confirmation is on.
    monkeypatch.setattr(sa, "_http_post", lambda p, b, **k: _Resp(200, {"id": "u9", "email": "new@x.com"}))
    assert sa.sign_up("new@x.com", "pw") is None


def test_refresh_returns_new_session(monkeypatch):
    captured = {}

    def _fake_post(path, body, **kw):
        captured["path"] = path
        return _Resp(200, _token_payload(access="A2", refresh="R2"))

    monkeypatch.setattr(sa, "_http_post", _fake_post)
    s = sa.refresh("old-refresh")
    assert s.access_token == "A2"
    assert "grant_type=refresh_token" in captured["path"]


def test_send_password_reset_posts_recover(monkeypatch):
    captured = {}
    monkeypatch.setattr(sa, "_http_post", lambda p, b, **k: captured.update(path=p, body=b) or _Resp(200, {}))
    sa.send_password_reset("me@x.com")
    assert captured["path"].endswith("/recover")
    assert captured["body"] == {"email": "me@x.com"}


# ---------------------------------------------------------- token_store


def test_token_store_roundtrip(monkeypatch):
    store = {}
    monkeypatch.setattr(ts.keyring, "set_password", lambda svc, key, val: store.__setitem__((svc, key), val))
    monkeypatch.setattr(ts.keyring, "get_password", lambda svc, key: store.get((svc, key)))
    monkeypatch.setattr(ts.keyring, "delete_password", lambda svc, key: store.pop((svc, key), None))

    assert ts.load_refresh_token() is None
    ts.save_refresh_token("R-123")
    assert ts.load_refresh_token() == "R-123"
    ts.clear_refresh_token()
    assert ts.load_refresh_token() is None


# ---------------------------------------------------------- AuthManager


def test_sign_in_wires_proxy_token_provider(monkeypatch):
    monkeypatch.setattr(sa, "sign_in", lambda e, p: Session("ACC", "REF", "u1", e, 9999999999))
    monkeypatch.setattr(ts, "save_refresh_token", lambda t: None)

    m = AuthManager()
    m.sign_in("me@x.com", "pw")
    assert m.signed_in
    assert m.user_email == "me@x.com"
    # the engine's proxy now resolves the access token from this session
    assert llm_proxy._get_access_token() == "ACC"


def test_restore_from_keychain_silent_refresh(monkeypatch):
    monkeypatch.setattr(ts, "load_refresh_token", lambda: "STORED-REF")
    saved = {}
    monkeypatch.setattr(ts, "save_refresh_token", lambda t: saved.__setitem__("t", t))
    monkeypatch.setattr(sa, "refresh", lambda rt: Session("ACC2", "REF2", "u1", "me@x.com", 9999999999))

    m = AuthManager()
    assert m.restore() is True
    assert m.signed_in
    assert llm_proxy._get_access_token() == "ACC2"
    assert saved["t"] == "REF2"


def test_restore_clears_on_invalid_token(monkeypatch):
    monkeypatch.setattr(ts, "load_refresh_token", lambda: "BAD-REF")
    cleared = {"v": False}
    monkeypatch.setattr(ts, "clear_refresh_token", lambda: cleared.__setitem__("v", True))

    def _bad_refresh(rt):
        raise AuthError("invalid refresh token", status=400)

    monkeypatch.setattr(sa, "refresh", _bad_refresh)
    m = AuthManager()
    assert m.restore() is False
    assert not m.signed_in
    assert cleared["v"] is True


def test_sign_out_clears(monkeypatch):
    monkeypatch.setattr(sa, "sign_in", lambda e, p: Session("ACC", "REF", "u1", e, 9999999999))
    monkeypatch.setattr(ts, "save_refresh_token", lambda t: None)
    cleared = {"v": False}
    monkeypatch.setattr(ts, "clear_refresh_token", lambda: cleared.__setitem__("v", True))

    m = AuthManager()
    m.sign_in("me@x.com", "pw")
    m.sign_out()
    assert not m.signed_in
    assert cleared["v"] is True
    assert llm_proxy._get_access_token() is None


async def test_wired_refresher_renews_session_on_401(monkeypatch):
    # The refresher wired into llm_proxy (used by the 401 retry path) renews the
    # session and the provider then returns the fresh access token.
    monkeypatch.setattr(sa, "sign_in", lambda e, p: Session("ACC", "REF", "u1", e, 9999999999))
    monkeypatch.setattr(ts, "save_refresh_token", lambda t: None)
    monkeypatch.setattr(sa, "refresh", lambda rt: Session("ACC-NEW", "REF-NEW", "u1", "me@x.com", 9999999999))

    m = AuthManager()
    m.sign_in("me@x.com", "pw")
    assert llm_proxy._get_access_token() == "ACC"
    ran = await llm_proxy.refresh_access_token()  # invokes the wired refresher
    assert ran is True
    assert llm_proxy._get_access_token() == "ACC-NEW"
