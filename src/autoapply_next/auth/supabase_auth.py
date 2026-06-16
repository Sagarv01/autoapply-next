"""Supabase (GoTrue) auth via the REST API.

We talk to GoTrue directly with httpx (not the supabase-py SDK) so the calls are
small, synchronous-friendly for Qt, and trivially testable. The proxy validates
the resulting Supabase JWT via JWKS. We never store or auto-fill passwords: the
user types them, we exchange for tokens, and only the refresh token is persisted
(in the OS keychain, see token_store).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

# The AutoApply Supabase project. URL + anon key are public (the anon key is a
# publishable key, safe to embed); overridable via env for staging/self-host.
_DEFAULT_SUPABASE_URL = "https://ndkeryoqlvktzuzxubvb.supabase.co"
_DEFAULT_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5ka2VyeW9xbHZrdHp1enh1YnZiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYxNjI0MTgsImV4cCI6MjA5MTczODQxOH0."
    "aknkqtGQjQHK8-ARZWE0WLHrl2AKAD7y8nind37J9-o"
)


def _supabase_url() -> str:
    return (os.environ.get("AUTOAPPLY_SUPABASE_URL") or _DEFAULT_SUPABASE_URL).rstrip("/")


def _anon_key() -> str:
    return os.environ.get("AUTOAPPLY_SUPABASE_ANON_KEY") or _DEFAULT_ANON_KEY


class AuthError(RuntimeError):
    """A GoTrue auth failure (bad credentials, network, etc.)."""

    def __init__(self, message: str, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str
    user_id: str
    email: str
    expires_at: int  # unix seconds


def _http_post(path: str, body: dict, *, timeout: float = 20.0) -> httpx.Response:
    """POST to GoTrue with the anon apikey. Factored so tests can stub it."""
    url = f"{_supabase_url()}{path}"
    headers = {"apikey": _anon_key(), "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as client:
            return client.post(url, headers=headers, json=body)
    except httpx.RequestError as e:
        raise AuthError(f"Network error reaching sign-in: {e}", status=503) from e


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    detail: Any
    try:
        detail = resp.json()
    except Exception:
        detail = {"raw": resp.text[:300]}
    msg = ""
    if isinstance(detail, dict):
        msg = (
            detail.get("error_description")
            or detail.get("msg")
            or detail.get("message")
            or detail.get("error")
            or ""
        )
    raise AuthError(msg or f"Auth failed ({resp.status_code})", status=resp.status_code, detail=detail)


def _session_from_token_payload(data: dict) -> Session:
    user = data.get("user") or {}
    expires_at = data.get("expires_at")
    if not expires_at:
        expires_at = int(time.time()) + int(data.get("expires_in") or 3600)
    return Session(
        access_token=str(data["access_token"]),
        refresh_token=str(data["refresh_token"]),
        user_id=str(user.get("id") or ""),
        email=str(user.get("email") or ""),
        expires_at=int(expires_at),
    )


def sign_in(email: str, password: str) -> Session:
    resp = _http_post("/auth/v1/token?grant_type=password", {"email": email, "password": password})
    _raise_for_status(resp)
    return _session_from_token_payload(resp.json())


def sign_up(email: str, password: str, full_name: str = "") -> Session | None:
    """Create an account. Returns a Session if Supabase returns one immediately,
    or None when email confirmation is required (the user must confirm first)."""
    resp = _http_post(
        "/auth/v1/signup",
        {"email": email, "password": password, "data": {"full_name": full_name}},
    )
    _raise_for_status(resp)
    data = resp.json() or {}
    if data.get("access_token"):
        return _session_from_token_payload(data)
    return None


def refresh(refresh_token: str) -> Session:
    resp = _http_post("/auth/v1/token?grant_type=refresh_token", {"refresh_token": refresh_token})
    _raise_for_status(resp)
    return _session_from_token_payload(resp.json())


def send_password_reset(email: str) -> None:
    resp = _http_post("/auth/v1/recover", {"email": email})
    _raise_for_status(resp)
