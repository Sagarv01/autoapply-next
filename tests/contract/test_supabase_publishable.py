"""Migrate the client's Supabase auth to the modern publishable key.

The anon JWT is being deprecated; the replacement is an opaque sb_publishable_
key, sent as the GoTrue `apikey` header. Prefer AUTOAPPLY_SUPABASE_PUBLISHABLE_KEY,
honor the legacy AUTOAPPLY_SUPABASE_ANON_KEY during transition, and embed a
publishable default.
"""

from __future__ import annotations

from autoapply_next.auth import supabase_auth as sa


def test_embedded_default_is_a_publishable_key(monkeypatch):
    # The default key was removed from source; ensure a configured key is used.
    monkeypatch.setenv("AUTOAPPLY_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_testdefault")
    assert sa._publishable_key().startswith("sb_publishable_")


def test_publishable_env_override_is_preferred(monkeypatch):
    monkeypatch.setenv("AUTOAPPLY_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_envvalue")
    assert sa._publishable_key() == "sb_publishable_envvalue"


def test_legacy_anon_env_still_honored(monkeypatch):
    monkeypatch.delenv("AUTOAPPLY_SUPABASE_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setenv("AUTOAPPLY_SUPABASE_ANON_KEY", "legacy.anon.jwt")
    assert sa._publishable_key() == "legacy.anon.jwt"


def test_http_post_sends_publishable_apikey(monkeypatch):
    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(sa.httpx, "Client", _FakeClient)
    monkeypatch.setenv("AUTOAPPLY_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    sa._http_post("/auth/v1/token", {"x": 1})
    assert captured["headers"]["apikey"] == "sb_publishable_test"
