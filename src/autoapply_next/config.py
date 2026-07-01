"""Central runtime configuration for the desktop app.

All URLs, keys, and infrastructure endpoints live here so they can be
overridden via environment variables without rebuilding. Functions in this
module read the current environment each time they are called, so tests can
set env vars after import and still see the override.
"""
from __future__ import annotations

import os


def _env_or_call(name: str, default: str) -> str:
    return (os.environ.get(name) or default).rstrip("/")


# Production defaults. Override via environment variables for staging/self-host.
_DEFAULT_SUPABASE_URL = "https://ndkeryoqlvktzuzxubvb.supabase.co"
_DEFAULT_PUBLISHABLE_KEY = "sb_publishable_de9WAIQZ9jYJOuWZSFGHrw_GydeitzH"
_DEFAULT_PROXY_BASE_URL = "https://autoapply-proxy-799883391199.australia-southeast1.run.app"
_DEFAULT_DOWNLOAD_URL = "https://autoapply.com.au/download"
_DEFAULT_SEEK_LOGIN_URL = "https://au.seek.com/oauth/login"
_DEFAULT_SEEK_VERIFY_URL = "https://au.seek.com/profile/me"


def supabase_url() -> str:
    return _env_or_call("AUTOAPPLY_SUPABASE_URL", _DEFAULT_SUPABASE_URL)


def supabase_publishable_key() -> str:
    return (
        os.environ.get("AUTOAPPLY_SUPABASE_PUBLISHABLE_KEY")
        or os.environ.get("AUTOAPPLY_SUPABASE_ANON_KEY")
        or _DEFAULT_PUBLISHABLE_KEY
    )


def proxy_base_url() -> str:
    return _env_or_call("AUTOAPPLY_PROXY_URL", _DEFAULT_PROXY_BASE_URL)


def download_url() -> str:
    return _env_or_call("AUTOAPPLY_DOWNLOAD_URL", _DEFAULT_DOWNLOAD_URL)


def seek_login_url() -> str:
    return _env_or_call("AUTOAPPLY_SEEK_LOGIN_URL", _DEFAULT_SEEK_LOGIN_URL)


def seek_verify_url() -> str:
    return _env_or_call("AUTOAPPLY_SEEK_VERIFY_URL", _DEFAULT_SEEK_VERIFY_URL)
