"""Refresh-token storage in the OS keychain (keyring).

Only the long-lived refresh token is persisted, and only in the platform secret
store (macOS Keychain / Windows Credential Locker via `keyring`). The short-lived
access token lives in memory only. Passwords are never stored.
"""
from __future__ import annotations

import logging

import keyring

logger = logging.getLogger(__name__)

_SERVICE = "autoapply-next"
_KEY = "supabase_refresh_token"


def save_refresh_token(token: str) -> None:
    try:
        keyring.set_password(_SERVICE, _KEY, token)
    except Exception as exc:  # keychain unavailable / locked
        logger.warning("token_store: could not save refresh token: %s", exc)


def load_refresh_token() -> str | None:
    try:
        return keyring.get_password(_SERVICE, _KEY)
    except Exception as exc:
        logger.warning("token_store: could not read refresh token: %s", exc)
        return None


def clear_refresh_token() -> None:
    try:
        keyring.delete_password(_SERVICE, _KEY)
    except Exception:
        # delete_password raises if the entry is absent; that's fine.
        pass
