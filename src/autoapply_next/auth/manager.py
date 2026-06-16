"""AuthManager: the app's single source of auth truth.

Holds the current Supabase session, persists the refresh token to the OS
keychain, and wires the engine's `llm_proxy` access-token provider + refresher
so every proxy LLM call is authenticated. On app start, `restore()` silently
re-signs-in from the stored refresh token; on a 401 the engine's retry path
calls the wired refresher to renew the access token.
"""
from __future__ import annotations

import logging

from autoapply_next.engine import llm_proxy

from . import supabase_auth, token_store
from .supabase_auth import AuthError, Session

logger = logging.getLogger(__name__)


class AuthManager:
    def __init__(self) -> None:
        self._session: Session | None = None

    # ---------------------------------------------------------- properties
    @property
    def signed_in(self) -> bool:
        return self._session is not None

    @property
    def user_email(self) -> str | None:
        return self._session.email if self._session else None

    @property
    def user_id(self) -> str | None:
        return self._session.user_id if self._session else None

    # ---------------------------------------------------------- wiring
    def _apply(self, session: Session) -> None:
        self._session = session
        token_store.save_refresh_token(session.refresh_token)
        llm_proxy.set_access_token_provider(self._access_token)
        llm_proxy.set_token_refresher(self._refresh)

    def _access_token(self) -> str | None:
        return self._session.access_token if self._session else None

    def _refresh(self) -> None:
        """Renew the access token from the stored refresh token. Wired into
        llm_proxy as the 401 refresher (sync; llm_proxy awaits if needed)."""
        if not self._session:
            return
        self._session = supabase_auth.refresh(self._session.refresh_token)
        token_store.save_refresh_token(self._session.refresh_token)

    # ---------------------------------------------------------- actions
    def sign_in(self, email: str, password: str) -> Session:
        self._apply(supabase_auth.sign_in(email, password))
        return self._session  # type: ignore[return-value]

    def sign_up(self, email: str, password: str, full_name: str = "") -> Session | None:
        session = supabase_auth.sign_up(email, password, full_name)
        if session is not None:
            self._apply(session)
        return session  # None => email confirmation required

    def send_password_reset(self, email: str) -> None:
        supabase_auth.send_password_reset(email)

    def restore(self) -> bool:
        """Silently re-sign-in from the keychain refresh token. Returns True on
        success; on an invalid/expired token, clears it and returns False."""
        rt = token_store.load_refresh_token()
        if not rt:
            return False
        try:
            self._apply(supabase_auth.refresh(rt))
            return True
        except AuthError as exc:
            logger.info("AuthManager.restore: stored token invalid (%s); clearing", exc)
            token_store.clear_refresh_token()
            self._session = None
            return False

    def sign_out(self) -> None:
        self._session = None
        token_store.clear_refresh_token()
        llm_proxy.set_access_token_provider(None)
        llm_proxy.set_token_refresher(None)
