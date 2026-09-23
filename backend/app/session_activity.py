"""Loss-tolerant activity hints, separate from evidence and request transactions."""

from __future__ import annotations

import inspect
import logging
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
from weakref import WeakKeyDictionary, finalize

from sqlalchemy import create_engine, or_, update
from sqlalchemy.orm import Session

from app.db import DB_POOL_RECYCLE, PG_CONNECT_ARGS
from app.models import GameSession
from app.opportunity_retention import database_clock, shifted_clock

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_inflight = Counter()
_hint_engines = WeakKeyDictionary()


def _hint_engine(engine):
    if engine.dialect.name != "postgresql":
        return engine
    # Request handlers can still own a read connection after their core commit.
    # Never queue for a second slot in that same pool: a saturated request pool
    # would leave every response waiting for another response to release a slot.
    with _lock:
        if engine not in _hint_engines:
            hint = create_engine(
                engine.url, pool_size=1, max_overflow=0, pool_timeout=0,
                pool_recycle=DB_POOL_RECYCLE, pool_pre_ping=True,
                connect_args={**PG_CONNECT_ARGS, "connect_timeout": 2},
            )
            _hint_engines[engine] = hint
            finalize(engine, hint.dispose)
        return _hint_engines[engine]


def dispose_activity_engine(engine):
    with _lock:
        hint = _hint_engines.pop(engine, None)
    if hint is not None:
        hint.dispose()


@contextmanager
def session_work(user_id):
    """Only a local hint. Database try-locks arbitrate cross-process work."""
    with _lock:
        _inflight[user_id] += 1
    try:
        yield
    finally:
        with _lock:
            _inflight[user_id] -= 1
            if not _inflight[user_id]:
                del _inflight[user_id]


def known_inflight(user_id: int) -> bool:
    with _lock:
        return bool(_inflight.get(user_id))


def stamp_activity(engine, *, session_id, user_id: int) -> bool:
    """Conditional, database-stamped UPDATE; never share a request transaction.

    Coalescing is in the database, so replicas agree and failed writes can retry.
    The conditional UPDATE takes no row lock for a coalesced hint. A 1ms lock
    timeout bounds contention with uploads; a missed hint never changes them.
    """
    try:
        engine = _hint_engine(engine)
        with Session(bind=engine.execution_options(session_activity_hint=True)) as db:
            if engine.dialect.name == "postgresql":
                from app.opportunity_fold import _set_configs
                _set_configs(db, statement_timeout="100ms", lock_timeout="1ms",
                             idle_in_transaction_session_timeout="100ms")
            due = (
                GameSession.id == session_id,
                GameSession.user_id == user_id,
                or_(GameSession.last_activity_at.is_(None),
                    GameSession.last_activity_at <= shifted_clock(db, timedelta(minutes=1))),
            )
            result = db.execute(update(GameSession).where(*due).values(
                last_activity_at=database_clock(db),
            ))
            db.commit()
            return result.rowcount > 0
    except Exception:
        logger.warning("session_activity_hint_failed", exc_info=True)
        return False


def records_session_activity(endpoint):
    """Cover every successful branch, including replay and idempotent returns.

    Apply immediately beneath the FastAPI route decorator. Resolve annotations
    in the endpoint's namespace before wrapping, for FastAPI dependency parsing.
    """
    signature = inspect.signature(endpoint, eval_str=True)

    @wraps(endpoint)
    def wrapped(*args, **kwargs):
        values = signature.bind(*args, **kwargs).arguments
        user_id = values["user"].user_id
        with session_work(user_id):
            result = endpoint(*args, **kwargs)
            try:
                session_id = (values.get("session_id")
                              or getattr(values.get("request"), "session_id", None)
                              or getattr(result, "session_id", None))
                bind = values["db"].get_bind()
                engine = getattr(bind, "engine", bind)
                if session_id is not None:
                    stamp_activity(engine, session_id=session_id, user_id=user_id)
            except Exception:
                # Even hint setup must not turn already-committed work into 500.
                logger.warning("session_activity_hint_failed", exc_info=True)
            return result

    wrapped.__signature__ = signature
    return wrapped
