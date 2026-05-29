"""Contract tests for :mod:`autoapply_next.safe_logging.scrubber`.

Each test logs a message that contains a fake instance of one of the PII
or secret kinds the scrubber is supposed to redact, captures the formatted
record, and asserts:

1. The fake PII does NOT appear in the output.
2. The corresponding ``<REDACTED:KIND>`` token DOES appear.

Plus tests that ``install_global_scrubbing`` is idempotent and that
non-sensitive log lines pass through unchanged.
"""

from __future__ import annotations

import io
import logging

import pytest

from autoapply_next.safe_logging.scrubber import (
    PIIScrubFilter,
    install_global_scrubbing,
)


# Fake values used across tests. None of these are real credentials.
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
FAKE_ANTHROPIC_KEY = "sk-ant-api03-AAAA1111BBBB2222CCCC3333DDDD4444EEEE"
FAKE_OPENAI_KEY = "sk-AAAA1111BBBB2222CCCC3333DDDD4444"
FAKE_EMAIL = "candidate.person@example.com"
FAKE_PHONE_INTL = "+61 491 621 148"
FAKE_PHONE_LOCAL = "0491 621 148"
FAKE_PHONE_GENERIC = "+1 555 123 4567"
FAKE_COOKIE_HEADER = "Cookie: seek-session=abc123def456ghi789; other=keep"
FAKE_PASSWORD_URL = "https://example.com/login?password=hunter2&next=/home"
FAKE_TOKEN_URL = "https://example.com/cb?token=opaque-token-value&state=ok"
FAKE_BLOB = (
    "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8S9T0"
    "U1V2W3X4Y5Z6a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"
)


@pytest.fixture
def capturing_logger() -> tuple[logging.Logger, io.StringIO]:
    """A logger with its own StringIO handler and the PII scrub filter."""

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(PIIScrubFilter())

    logger = logging.getLogger("autoapply_next.tests.pii")
    # Wipe state from prior tests.
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, buf


def _emit(logger: logging.Logger, buf: io.StringIO, msg: str, *args) -> str:
    """Log ``msg`` and return the captured output, clearing the buffer."""

    logger.info(msg, *args)
    out = buf.getvalue()
    buf.seek(0)
    buf.truncate()
    return out


def test_jwt_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "auth token = %s", FAKE_JWT)
    assert FAKE_JWT not in out
    assert "<REDACTED:JWT>" in out


def test_anthropic_key_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "calling claude with key %s", FAKE_ANTHROPIC_KEY)
    assert FAKE_ANTHROPIC_KEY not in out
    assert "<REDACTED:ANTHROPIC_KEY>" in out


def test_openai_key_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "openai key=%s", FAKE_OPENAI_KEY)
    assert FAKE_OPENAI_KEY not in out
    assert "<REDACTED:OPENAI_KEY>" in out


def test_email_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "applying as %s", FAKE_EMAIL)
    assert FAKE_EMAIL not in out
    assert "<REDACTED:EMAIL>" in out


def test_phone_intl_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "candidate phone %s", FAKE_PHONE_INTL)
    assert FAKE_PHONE_INTL not in out
    assert "<REDACTED:PHONE>" in out


def test_phone_local_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "candidate phone %s", FAKE_PHONE_LOCAL)
    assert FAKE_PHONE_LOCAL not in out
    assert "<REDACTED:PHONE>" in out


def test_phone_generic_intl_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "us phone %s", FAKE_PHONE_GENERIC)
    assert FAKE_PHONE_GENERIC not in out
    assert "<REDACTED:PHONE>" in out


def test_cookie_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, FAKE_COOKIE_HEADER)
    assert "abc123def456ghi789" not in out
    assert "<REDACTED:COOKIE>" in out
    # The cookie name should remain so we can still tell which cookie was
    # being logged.
    assert "seek-session" in out
    # Non-sensitive cookie key passes through.
    assert "other=keep" in out


def test_password_query_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "redirected to %s", FAKE_PASSWORD_URL)
    assert "hunter2" not in out
    assert "<REDACTED:SECRET>" in out
    # The "password=" prefix is preserved for debugging context.
    assert "password=" in out


def test_token_query_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "callback %s", FAKE_TOKEN_URL)
    assert "opaque-token-value" not in out
    assert "<REDACTED:SECRET>" in out
    assert "token=" in out


def test_long_blob_redacted(capturing_logger):
    logger, buf = capturing_logger
    out = _emit(logger, buf, "opaque blob: %s", FAKE_BLOB)
    assert FAKE_BLOB not in out
    assert "<REDACTED:BLOB>" in out


def test_normal_message_passes_through(capturing_logger):
    logger, buf = capturing_logger
    msg = "Processing job 12345 for board=seek with skills=python,aws"
    out = _emit(logger, buf, msg)
    # The whole message survives intact.
    assert msg in out
    # And there are no spurious redaction markers.
    assert "<REDACTED:" not in out


def test_install_global_scrubbing_is_idempotent():
    """Calling install_global_scrubbing twice must not double-redact."""

    # Reset root logger state so we can observe it cleanly.
    root = logging.getLogger()
    original_filters = list(root.filters)
    original_level = root.level
    try:
        # Wipe any filters left over from prior tests in this process.
        for f in list(root.filters):
            root.removeFilter(f)

        install_global_scrubbing()
        install_global_scrubbing()
        install_global_scrubbing()

        # The PII filter should be present exactly once on the root logger.
        pii_filters = [f for f in root.filters if isinstance(f, PIIScrubFilter)]
        assert len(pii_filters) == 1, (
            f"expected 1 PIIScrubFilter on root, got {len(pii_filters)}"
        )

        # And on each of the third-party loggers.
        for name in (
            "urllib3",
            "requests",
            "httpx",
            "anthropic",
            "openai",
            "playwright",
            "asyncio",
        ):
            lg = logging.getLogger(name)
            pii_filters = [f for f in lg.filters if isinstance(f, PIIScrubFilter)]
            assert len(pii_filters) == 1, (
                f"expected 1 PIIScrubFilter on {name}, got {len(pii_filters)}"
            )

        # Now verify that emitting a message through a downstream logger
        # produces a single redaction, not a doubled one.
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        try:
            test_logger = logging.getLogger("autoapply_next.tests.idempotent")
            test_logger.propagate = True
            test_logger.info("hello %s world", FAKE_EMAIL)
            out = buf.getvalue()
        finally:
            root.removeHandler(handler)

        assert FAKE_EMAIL not in out
        # Exactly one redaction marker, not two.
        assert out.count("<REDACTED:EMAIL>") == 1
    finally:
        # Restore root logger state.
        for f in list(root.filters):
            root.removeFilter(f)
        for f in original_filters:
            root.addFilter(f)
        root.setLevel(original_level)


def test_filter_returns_true_always(capturing_logger):
    """The filter must never suppress records."""

    f = PIIScrubFilter()
    rec = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"contains {FAKE_EMAIL} please redact",
        args=(),
        exc_info=None,
    )
    assert f.filter(rec) is True
    # And the record's msg is redacted in-place.
    assert FAKE_EMAIL not in rec.getMessage()
    assert "<REDACTED:EMAIL>" in rec.getMessage()
