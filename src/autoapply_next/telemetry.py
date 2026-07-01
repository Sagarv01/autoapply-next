"""PostHog error tracking for the AutoApply Next desktop app.

This module wires the PostHog Python SDK for **error tracking only** —
exceptions and crashes — not product analytics. No page views, no session
recordings, no feature-flag person tracking. The only events sent are
exception captures from :func:`capture_exception` and the global
``sys.excepthook`` installed in :mod:`autoapply_next.safe_ui.error_handler`.

Graceful degradation
--------------------

The PostHog project API key is read from the ``POSTHOG_API_KEY`` environment
variable. If the key is absent (or the ``posthog`` package is not installed),
every function in this module becomes a no-op and the app runs exactly as it
did before — error tracking is strictly opt-in. A single DEBUG line is
emitted on startup so the absence is visible in the log without being noisy.
No telemetry function ever raises into the host code path.

Shutdown / flushing
-------------------

The SDK ships events on a background *consumer* thread (a daemon) and flushes
every ~0.5 s. It also registers its own ``atexit`` handler that calls the
client's blocking ``join()`` so queued events are drained on clean exit. We
do NOT add a second atexit hook — that would double-block on the same queue.
Because the SDK's ``flush()`` / ``join()`` have no timeout argument in the
installed 3.x–7.x line and block until the queue drains, :func:`flush` here
wraps the call in a short-lived daemon thread joined with a hard timeout, so
an explicit caller is never blocked longer than that. The constructor's
``timeout`` / ``max_retries`` are kept small so the SDK's own atexit drain is
bounded on a flaky network rather than hanging the app exit for 45+ seconds.

PII / secret considerations
---------------------------

PostHog's ``capture_exception`` ships the exception type, message, and a
stack trace of file / line / function frames. Local variable values are NOT
captured — ``capture_exception_code_variables`` defaults to ``False`` in the
SDK and we leave it that way, so candidate PII (emails, phone numbers,
session tokens) held in locals never leaves the machine. The exception
*message* itself may still contain PII if a library embeds it there; this is
the inherent trade-off of any crash reporter and is accepted here. The
structured-logging PII scrubber in :mod:`autoapply_next.safe_logging.scrubber`
continues to scrub on-disk logs independently of this module.

Cloud region
------------

PostHog Cloud US (``https://us.posthog.com``) is the default host. Override
with the ``POSTHOG_HOST`` env var for EU Cloud (``https://eu.posthog.com``)
or a self-hosted instance.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading
from typing import Any

try:
    from posthog import Posthog
except ImportError:  # pragma: no cover - exercised only when posthog is absent
    Posthog = None  # type: ignore[assignment, misc]

from . import __version__

logger = logging.getLogger(__name__)

# Module-level singleton. ``None`` means "not initialised / disabled" — every
# public function short-circuits to a no-op in that state so callers never
# need to check.
_client: Posthog | None = None

_DEFAULT_HOST = "https://us.posthog.com"
_DEFAULT_DISTINCT_ID = "autoapply-desktop"
# Hard cap on how long an explicit flush() may block the caller. The SDK's own
# flush()/join() have no timeout and block until the queue drains; we run them
# on a daemon thread and join for at most this long.
_FLUSH_TIMEOUT_SECONDS = 2
# Per-request HTTP timeout and retry count for the SDK consumer. Kept small so
# the SDK's atexit drain on a dead network is bounded (~max_retries * timeout)
# rather than hanging app exit for 45+ seconds. Error events are rare, so a
# 5 s budget is plenty for a real upload (typically <1 s).
_REQUEST_TIMEOUT_SECONDS = 5
_MAX_RETRIES = 2


def _app_context() -> dict[str, Any]:
    """Static metadata attached to every captured exception so PostHog can
    slice errors by version / platform. Read fresh each call so env-driven
    overrides and version bumps are picked up without re-import."""
    return {
        "app_name": "autoapply-next",
        "app_version": __version__,
        "platform": sys.platform,
        "python_version": platform.python_version(),
        "os": platform.platform(),
    }


def _distinct_id() -> str:
    """The PostHog ``distinct_id`` for error events.

    Defaults to a single anonymous constant (``autoapply-desktop``) so all
    desktop crash reports attribute to one identity. PostHog Error Tracking
    groups issues by exception fingerprint, not by distinct_id, so a shared
    id does not merge unrelated bugs. Override per-machine with the
    ``POSTHOG_DISTINCT_ID`` env var if per-device attribution is later
    desired.
    """
    return os.environ.get("POSTHOG_DISTINCT_ID") or _DEFAULT_DISTINCT_ID


def _on_posthog_error(exc: Exception, data: Any) -> None:  # noqa: ARG001
    """PostHog upload-failure callback. Logged at WARNING but never raised —
    a telemetry outage must not perturb the host application."""
    logger.warning("posthog: upload failed: %s", exc)


def init_posthog() -> None:
    """Initialise the PostHog error-tracking client.

    Reads ``POSTHOG_API_KEY`` from the environment. When the key is missing
    (or the ``posthog`` package is unavailable) the function returns
    immediately after a DEBUG log line — the app continues without error
    tracking. Idempotent: a second call flushes and replaces the existing
    client (useful in tests).

    The client is configured for error tracking, not analytics:
    ``disable_geoip=True`` (no IP-based geo enrichment), local-variable
    capture left off (SDK default — no PII in tracebacks), and feature-flag
    local evaluation disabled (no background flag polling). Events are sent
    on the SDK's background consumer thread; the SDK registers its own
    ``atexit`` drain, so this function does NOT add a second one.
    """
    global _client

    # Tear down any prior client so re-init (e.g. in tests) is clean. Use the
    # bounded flush() so a stuck old client cannot block re-init.
    if _client is not None:
        _client = None
        flush()

    if Posthog is None:
        logger.debug(
            "posthog: SDK not installed; error tracking disabled "
            "(install the 'posthog' package to enable)"
        )
        return

    api_key = os.environ.get("POSTHOG_API_KEY")
    if not api_key:
        logger.debug("posthog: POSTHOG_API_KEY not set; error tracking disabled")
        return

    host = os.environ.get("POSTHOG_HOST") or _DEFAULT_HOST

    try:
        _client = Posthog(
            api_key,
            host=host,
            # Error tracking, not analytics. Disable server-side IP geo
            # enrichment so we do not ship location data.
            disable_geoip=True,
            # We do not use feature flags; stop the background flag-definition
            # poller so the only network traffic this client generates is
            # exception uploads.
            enable_local_evaluation=False,
            # Bound per-request latency and retries so the SDK's atexit drain
            # cannot hang app exit on a dead network.
            timeout=_REQUEST_TIMEOUT_SECONDS,
            max_retries=_MAX_RETRIES,
            on_error=_on_posthog_error,
        )
    except Exception as exc:
        # Never let telemetry init crash the app. Log and disable.
        logger.warning("posthog: failed to initialise client: %s", exc)
        _client = None
        return

    logger.info(
        "posthog: error tracking enabled (host=%s, distinct_id=%s)",
        host,
        _distinct_id(),
    )


def flush(timeout_seconds: float = _FLUSH_TIMEOUT_SECONDS) -> None:
    """Best-effort, **bounded** flush of the PostHog event queue.

    The PostHog SDK's ``flush()``/``join()`` take no timeout argument and
    block until the internal queue is fully drained — on a flaky network that
    can hang the caller indefinitely. To keep the host responsive we run the
    blocking flush on a short-lived daemon thread and join it for at most
    ``timeout_seconds``. Anything still in flight when the timeout elapses is
    left to the SDK's background consumer (and its own atexit drain) to
    deliver.

    No-op when tracking is disabled; never raises.
    """
    if _client is None:
        return

    def _worker() -> None:
        try:
            _client.flush()
        except Exception as exc:  # noqa: PERF203 - keep the worker alive
            logger.warning("posthog: flush failed: %s", exc)

    worker = threading.Thread(target=_worker, name="posthog-flush", daemon=True)
    worker.start()
    worker.join(timeout_seconds)


def capture_exception(
    error: BaseException | None,
    properties: dict[str, Any] | None = None,
) -> None:
    """Send an exception to PostHog Error Tracking.

    Parameters
    ----------
    error:
        The exception instance to capture. Pass ``None`` to capture the
        current exception via ``sys.exc_info()`` (useful inside ``except``
        blocks where the live traceback is already bound).
    properties:
        Extra event properties, merged on top of the app context (version /
        platform). Caller-supplied keys win over the defaults.

    The event is queued for the SDK's background consumer; it is flushed
    either on the consumer's ~0.5 s interval or by the SDK's atexit drain at
    process exit. Call :func:`flush` afterwards if you need a best-effort
    synchronous send (e.g. right before a deliberate exit).

    Gracefully degrades: a no-op when PostHog is not initialised (key not
    set or SDK missing). Never raises.
    """
    if _client is None:
        return
    try:
        props = _app_context()
        if properties:
            props.update(properties)
        _client.capture_exception(
            error,
            distinct_id=_distinct_id(),
            properties=props,
        )
    except Exception as exc:
        # Telemetry must never throw into the host code path.
        logger.warning("posthog: capture_exception failed: %s", exc)
