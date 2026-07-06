"""Background prewarm of the opening graph/roots singletons at startup.

The opening graph takes ~30s to build (opening_graph.py) and is created lazily
behind a single-flight lock, so without prewarm the FIRST opening request after
a cold deploy pays the full build in-thread (30-60s). This module kicks the
build off in a daemon thread during FastAPI startup so gameplay requests find
the singletons already warm. A request arriving mid-warm blocks on the same
single-flight lock and shares the in-progress build — prewarm never races or
duplicates the lazy path.

The thread must never block startup: Railway health-checks /health with a 30s
timeout and the lifespan body runs before the server accepts requests, so
start_prewarm() only spawns the thread and returns. Gated by PREWARM_OPENINGS
(explicit true/false wins); when unset it defaults on only on deploy platforms
(Railway/Render) so tests and local dev never eat the build.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from app.database_url import is_deploy_platform
from app.opening_roots import get_opening_roots

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def prewarm_enabled() -> bool:
    raw = os.getenv("PREWARM_OPENINGS")
    if raw is not None:
        value = raw.strip().lower()
        if value in _TRUE_VALUES:
            return True
        if value in _FALSE_VALUES:
            return False
        logger.warning(
            "PREWARM_OPENINGS=%r not recognized; falling back to platform default",
            raw,
        )
    return is_deploy_platform()


def prewarm_openings() -> None:
    """Force the opening roots (and, transitively, graph) singletons warm.

    Never raises: on failure the singletons stay None (build assigns only on
    success), so the next request retries via the existing lazy path.
    """
    started = time.monotonic()
    logger.info("opening prewarm: starting")
    try:
        get_opening_roots()
    except Exception:
        logger.exception("opening prewarm failed")
        return
    logger.info(
        "opening prewarm: roots ready in %.1fs", time.monotonic() - started
    )


def start_prewarm() -> threading.Thread | None:
    """Spawn the prewarm daemon thread (not joined; never blocks the caller).

    Returns the thread, or None when prewarm is gated off.
    """
    if not prewarm_enabled():
        logger.info("opening prewarm: disabled; openings build lazily on first use")
        return None
    thread = threading.Thread(
        target=prewarm_openings, name="opening-prewarm", daemon=True
    )
    thread.start()
    return thread
