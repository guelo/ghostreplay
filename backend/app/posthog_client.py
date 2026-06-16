"""Process-singleton PostHog client + no-op-when-disabled helpers.

Analytics must NEVER break a request: every helper here swallows all exceptions
and degrades to a no-op when disabled. The disable decision lives HERE (token
missing or ``POSTHOG_DISABLED=true``), never relying on an SDK ``disabled=``
kwarg. Capture is fire-and-forget on the SDK's background sender thread; call
``shutdown()`` from the app lifespan teardown so queued events aren't dropped on
deploy/restart.
"""

from __future__ import annotations

import logging
import os
import threading
import time

try:  # A missing/broken SDK must never prevent the app from starting.
    from posthog import Posthog
except Exception:  # pragma: no cover - defensive import guard
    Posthog = None  # type: ignore[assignment, misc]

logger = logging.getLogger("ghostreplay.analytics")

ANON_DISTINCT_ID = "anon"
DEFAULT_HOST = "https://us.i.posthog.com"
SLOW_CAPTURE_LOG_MS = 250

_client: "Posthog | None" = None
_initialized = False
_lock = threading.Lock()


def _disabled() -> bool:
    return os.environ.get("POSTHOG_DISABLED", "").strip().lower() == "true"


def get_client() -> "Posthog | None":
    """Return the process-singleton client, or ``None`` when analytics is off.

    Returns ``None`` (so every helper no-ops) when the SDK is unavailable,
    ``POSTHOG_DISABLED=true``, or ``POSTHOG_PROJECT_TOKEN`` is unset. The client
    is built once and cached for the process lifetime.
    """
    global _client, _initialized
    if _initialized:
        return _client
    with _lock:
        if _initialized:
            return _client
        token = os.environ.get("POSTHOG_PROJECT_TOKEN")
        if Posthog is None or _disabled() or not token:
            _client = None
        else:
            host = os.environ.get("POSTHOG_HOST") or DEFAULT_HOST
            try:
                _client = Posthog(token, host=host)
            except Exception:
                logger.exception("posthog client init failed; analytics disabled")
                _client = None
        _initialized = True
    return _client


def capture(distinct_id: str | None, event: str, properties: dict | None = None) -> None:
    """Capture an event. No-ops when disabled and never raises into the caller.

    ``distinct_id`` falls back to ``"anon"`` when not provided. The SDK is called
    with all keyword arguments so call sites are insulated from positional
    signature drift between SDK versions.
    """
    started = time.perf_counter()
    try:
        client = get_client()
        if client is None:
            return
        try:
            client.capture(
                distinct_id=distinct_id or ANON_DISTINCT_ID,
                event=event,
                properties=properties or {},
            )
        except Exception:
            logger.debug("posthog capture failed for event %s", event, exc_info=True)
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= SLOW_CAPTURE_LOG_MS:
            logger.info("posthog capture slow event=%s duration_ms=%.3f", event, elapsed_ms)


def shutdown() -> None:
    """Flush and join the background sender thread. No-ops and never raises."""
    client = get_client()
    if client is None:
        return
    try:
        fn = getattr(client, "shutdown", None)
        if callable(fn):
            fn()
    except Exception:
        logger.debug("posthog shutdown failed", exc_info=True)


def _reset() -> None:
    """Drop the cached singleton. Intended for tests that flip env vars."""
    global _client, _initialized
    with _lock:
        if _client is not None:
            try:
                shutdown_fn = getattr(_client, "shutdown", None)
                if callable(shutdown_fn):
                    shutdown_fn()
            except Exception:
                pass
        _client = None
        _initialized = False
