"""PII and secret scrubbing for log records.

The engine logs to stdout, stderr, and ``bot.log``; the GUI tails those logs
so the operator can see what the bot is doing. Anything that ends up in a
log line ends up on the operator's screen, in the packaged log file, and in
any future crash report we upload. We do not want PII or secrets in any of
those channels.

What this filter targets and why
--------------------------------

- Supabase JWTs (3-segment dotted base64): the auth slice handles these, but
  third-party HTTP libraries occasionally log ``Authorization`` headers when
  retrying or when the verbose flag flips on.
- Anthropic and OpenAI API keys: the ``anthropic`` and ``openai`` SDKs log
  request previews at DEBUG; if our log level ever leaks to DEBUG in prod we
  do not want the key in the file.
- Email addresses: the engine's ``applicator`` writes the candidate email
  into form-fill log lines for debugging.
- Australian phone numbers: same path as email; the candidate's phone shows
  up in ``profile.txt`` and gets echoed during form fill.
- ``seek-*`` / ``auth-*`` cookies: ``playwright``, ``urllib3``, and
  ``requests`` all log cookie jars at DEBUG. Seek's session cookies are
  long-lived bearer tokens for the operator's account.
- ``password=`` / ``token=`` query params: occasionally URLs end up in
  exception messages and stack traces.
- Long base64-looking blobs (> 80 chars): catch-all for opaque session
  tokens or screenshots-as-base64 that some libraries dump.

Important caveat
----------------

This is defence in depth, NOT the only PII protection. The engine's
``bot.log`` file may still contain PII written before this filter ran (e.g.
from a previous version, or from any process that writes to that file
without installing this filter). Treat ``bot.log`` as sensitive on disk.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTION_TEMPLATE = "<REDACTED:{kind}>"


# Each rule is (kind, compiled regex). Order matters: more specific patterns
# must run first so that, e.g., a JWT is tagged as JWT rather than as the
# generic SECRET that follows ``token=``, and an Anthropic key is not first
# matched as the more permissive OpenAI key pattern.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    # JWTs (3 dotted base64url segments). Catches Supabase access tokens and
    # most OIDC bearer tokens. Runs BEFORE the SECRET rule so that strings
    # like ``token=eyJ...`` get classified as JWT rather than SECRET.
    (
        "JWT",
        re.compile(
            r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"
        ),
    ),
    # Anthropic API keys: sk-ant-...
    (
        "ANTHROPIC_KEY",
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ),
    # OpenAI keys: sk-... but NOT sk-ant- (handled above; the negative
    # lookahead prevents this rule from re-eating an already-redacted
    # Anthropic key or matching the redaction token itself).
    (
        "OPENAI_KEY",
        re.compile(r"sk-(?!ant-)[A-Za-z0-9]{20,}"),
    ),
    # Cookie values for sensitive cookie names. We match either
    # ``Cookie: ...`` / ``Set-Cookie: ...`` headers or bare
    # ``seek-foo=value`` / ``auth-foo=value`` pairs anywhere in the line, and
    # redact JUST the value, leaving the cookie name intact for debugging.
    # The value char class excludes ``<`` / ``>`` so we never overwrite an
    # earlier redaction marker.
    (
        "COOKIE",
        re.compile(
            r"((?:seek|auth)-[A-Za-z0-9_\-]+\s*=\s*)([^;\s,\"'<>]+)",
            re.IGNORECASE,
        ),
    ),
    # password= / token= query string or form-data values. Runs AFTER JWT
    # so that bearer tokens that happen to appear in ``token=`` slots are
    # still classified as JWT. The value char class excludes ``<`` and ``>``
    # so we never overwrite an earlier ``<REDACTED:...>`` marker that an
    # upstream rule (e.g. JWT) has already inserted.
    (
        "SECRET",
        re.compile(
            r"((?:password|token)\s*=\s*)([^&\s;,\"'<>]+)",
            re.IGNORECASE,
        ),
    ),
    # Email addresses.
    (
        "EMAIL",
        re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    ),
    # Australian phone numbers: +61 4xx xxx xxx and 04xx xxx xxx and
    # generic international +CC ... patterns. Order: most specific AU
    # patterns first, then generic international.
    (
        "PHONE",
        re.compile(r"\+?61\s?\d{3}\s?\d{3}\s?\d{3}"),
    ),
    (
        "PHONE",
        re.compile(r"\b04\d{2}\s?\d{3}\s?\d{3}\b"),
    ),
    (
        "PHONE",
        re.compile(r"\+\d{1,3}\s?\d{3,}\s?\d{3,}\s?\d{3,}"),
    ),
    # Long base64-looking blobs (catch-all for opaque tokens, screenshot
    # data URIs, etc.). 80+ chars of [A-Za-z0-9+/=_-].
    (
        "BLOB",
        re.compile(r"[A-Za-z0-9+/=_\-]{80,}"),
    ),
]


# Rules that capture a prefix in group 1 and the secret in group 2. These
# preserve the prefix in the output so the line still says ``password=`` or
# ``seek-foo=`` for debugging context.
_CAPTURING_KINDS: frozenset[str] = frozenset({"COOKIE", "SECRET"})


def _redact(text: str) -> str:
    """Apply every rule in order and return the redacted text."""

    for kind, pattern in _RULES:
        token = REDACTION_TEMPLATE.format(kind=kind)
        if kind in _CAPTURING_KINDS:
            text = pattern.sub(lambda m, t=token: m.group(1) + t, text)
        else:
            text = pattern.sub(token, text)
    return text


class PIIScrubFilter(logging.Filter):
    """Logging filter that scrubs PII and secrets from formatted records.

    Rewrites ``record.msg`` to the fully-formatted, scrubbed string and
    clears ``record.args`` so downstream handlers/formatters do not attempt
    to re-format with the original values. Also scrubs any string-valued
    ``record.exc_text`` and a few common extra fields.

    Always returns ``True``: this filter never suppresses records, it only
    redacts.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            formatted = record.getMessage()
        except Exception:
            # If %-formatting blows up, fall back to scrubbing the raw msg.
            formatted = str(record.msg)

        record.msg = _redact(formatted)
        record.args = None

        if record.exc_text:
            record.exc_text = _redact(record.exc_text)

        # Some libraries stash extras on the record. Scrub anything string-y.
        for attr in ("stack_info", "message"):
            val = getattr(record, attr, None)
            if isinstance(val, str):
                setattr(record, attr, _redact(val))

        return True


# Module-level singleton so install_global_scrubbing is idempotent: the same
# filter instance is added to each logger, and stdlib's
# ``Logger.addFilter`` is a no-op if the filter is already attached.
_FILTER_SINGLETON = PIIScrubFilter(name="autoapply_next.pii_scrub")

# Loggers we proactively attach the filter to. The root logger catches
# everything routed through stdlib logging, but third-party libraries that
# create their own logger hierarchy still propagate through root by default.
# Listing them explicitly is belt-and-braces in case anyone disables
# propagation upstream.
_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "urllib3",
    "requests",
    "httpx",
    "httpcore",
    "anthropic",
    "openai",
    "playwright",
    "asyncio",
)


def install_global_scrubbing(level: int = logging.INFO) -> None:
    """Install :class:`PIIScrubFilter` on the root logger and on common
    third-party loggers (``urllib3``, ``requests``, ``httpx``, ``anthropic``,
    ``openai``, ``playwright``, ``asyncio``).

    Idempotent. Calling this twice does not double-redact: the same filter
    instance is reused, and :meth:`logging.Logger.addFilter` deduplicates
    by identity.

    Also attaches the filter to every handler currently registered on the
    root logger and on those third-party loggers, because some handlers
    (e.g. ``QueueHandler`` and remote-shipping handlers) format the record
    inside the handler thread and would otherwise emit the original message.

    Sets the root logger's level to ``level`` if it has not been explicitly
    configured yet (i.e. still at ``WARNING``).
    """

    root = logging.getLogger()
    if root.level == logging.WARNING:
        root.setLevel(level)

    _attach(root)

    for name in _THIRD_PARTY_LOGGERS:
        _attach(logging.getLogger(name))


def _attach(logger: logging.Logger) -> None:
    """Attach the singleton filter to ``logger`` and all of its handlers."""

    logger.addFilter(_FILTER_SINGLETON)
    for handler in _iter_handlers(logger):
        handler.addFilter(_FILTER_SINGLETON)


def _iter_handlers(logger: logging.Logger) -> Iterable[logging.Handler]:
    """Yield every handler currently attached to ``logger``."""

    yield from list(logger.handlers)
