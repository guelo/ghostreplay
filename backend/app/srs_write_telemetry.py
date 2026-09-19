"""Optional, private, expiring SRS transaction observations (never SRS state).

Only a confirmed ROOT commit followed by an independent database clock sample
produces a successful completion bound. Enqueue time, event.created_at, and final
receipts are deliberately not substitutes. Missing samples remain explicit gaps.
See scripts/OBSERVE_SRS_WRITES.md for deployment and expiry requirements.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import threading
import time
import uuid

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
TTL_SECONDS = 45 * 86400
ENV = "GHOSTREPLAY_SRS_TELEMETRY_DIR"
_STATE = "srs_write_observations"
SOURCES = frozenset({
    "upload_ordinary", "upload_final", "upload_revised_line", "upload_legacy",
    "worker", "repair_session", "repair_all_sessions", "repair_blunder",
    "repair_all_blunders", "direct",
})


def forbidden_worktree_roots() -> list[Path]:
    """Keep repository discovery out of requests and independently testable."""
    repo = Path(__file__).resolve().parents[2]
    roots = [repo]
    if (repo / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        roots.extend(Path(line[9:]).resolve() for line in result.stdout.splitlines()
                     if line.startswith("worktree "))
    return roots


class PrivateStore:
    """Bounded private SQLite spool; no identifiers are sent to application logs."""

    def __init__(self, directory: str | Path, *, require_existing: bool = False):
        requested = Path(directory)
        if not requested.is_absolute() or requested.is_symlink():
            raise ValueError("telemetry directory must be absolute and not a symlink")
        self.directory = requested.resolve(strict=True)
        if stat.S_IMODE(self.directory.stat().st_mode) != 0o700:
            raise ValueError("telemetry directory must have mode 0700")
        if any(self.directory.is_relative_to(root) for root in forbidden_worktree_roots()):
            raise ValueError("private telemetry must be outside every worktree")
        if any(part in {"Documents", "Desktop", "Mobile Documents", "CloudStorage",
                        "Dropbox", "OneDrive", "Google Drive"}
               for part in self.directory.parts):
            raise ValueError("private telemetry must not be cloud synced")
        self.path = self.directory / "observations.sqlite3"
        flags = os.O_RDWR | os.O_NOFOLLOW | (0 if require_existing else os.O_CREAT)
        fd = os.open(self.path, flags, 0o600)
        try:
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise ValueError("private telemetry file must have mode 0600")
        finally:
            os.close(fd)
        self._prune_lock = threading.Lock()
        self._next_prune = 0.0
        with self.connect() as db:
            if require_existing:
                # An empty or unrelated SQLite file is not an observation spool.
                db.execute("SELECT id, session_id, started_at, sources, outcome, wrote, "
                           "database_at, received_at, expires_at, job_id FROM observations LIMIT 0")
                return
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, started_at TEXT,
                sources TEXT NOT NULL, outcome TEXT NOT NULL, wrote INTEGER NOT NULL,
                database_at TEXT, received_at REAL NOT NULL, expires_at REAL NOT NULL,
                job_id TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS observation_expiry ON observations(expires_at)")

    @contextmanager
    def connect(self):
        # Never silently recreate a file removed after startup/validation.
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0.1)
        try:
            db.execute("PRAGMA secure_delete = ON")
            db.execute("PRAGMA synchronous = NORMAL")
            with db:
                yield db
        finally:
            db.close()

    def prune(self, *, now: float | None = None) -> int:
        with self.connect() as db:
            count = db.execute("DELETE FROM observations WHERE expires_at <= ?",
                               (time.time() if now is None else now,)).rowcount
            db.commit()
            # secure_delete scrubs the DB; truncate WAL too, including frames
            # left by an interrupted previous expiry. Busy readers must alert.
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("telemetry expiry checkpoint busy")
            return count

    def expire_if_due(self) -> None:
        with self._prune_lock:
            if time.monotonic() < self._next_prune:
                return
            self.prune()
            self._next_prune = time.monotonic() + 3600

    def put(self, *, observation_id: str, session_id, sources, outcome: str,
            started_at=None, wrote=False, database_at=None, job_id=None,
            expires_at: float | None = None) -> None:
        if not set(sources) <= SOURCES:
            raise ValueError("unknown observation source")
        now = time.time()
        expiry = expires_at if expires_at is not None else now + TTL_SECONDS
        self.expire_if_due()
        with self.connect() as db:
            if expiry <= now:
                return
            # Updating an attempt NEVER renews its privacy deadline.
            db.execute("""INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  started_at=coalesce(excluded.started_at, observations.started_at),
                  sources=(SELECT json_group_array(value) FROM (
                    SELECT value FROM json_each(observations.sources)
                    UNION SELECT value FROM json_each(excluded.sources))),
                  outcome=excluded.outcome, wrote=excluded.wrote,
                  database_at=excluded.database_at
                WHERE CASE excluded.outcome
                    WHEN 'not_requested' THEN 0 WHEN 'pending' THEN 0
                    WHEN 'queued' THEN 1 WHEN 'worker_started' THEN 2 ELSE 3 END
                  >= CASE observations.outcome
                    WHEN 'not_requested' THEN 0 WHEN 'pending' THEN 0
                    WHEN 'queued' THEN 1 WHEN 'worker_started' THEN 2 ELSE 3 END""",
                (observation_id, str(session_id), _iso(started_at),
                 json.dumps(sorted(sources)), outcome, int(wrote), _iso(database_at),
                 now, expiry, job_id))

    def iter_rows(self):
        self.prune()
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            for row in db.execute("SELECT * FROM observations"):
                yield dict(row)

    def rows(self) -> list[dict]:
        """Small diagnostic/test surface; reports stream iter_rows instead."""
        return list(self.iter_rows())


def _iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()


_store: PrivateStore | None = None
_store_path: str | None = None
_store_lock = threading.Lock()
_failures = 0


def get_store() -> PrivateStore | None:
    global _store, _store_path
    path = os.environ.get(ENV)
    if not path:
        return None
    with _store_lock:
        if _store_path != path:
            _store_path = path
            _store = None
            try:
                _store = PrivateStore(path)
                _store.expire_if_due()
            except Exception:
                _store = None
                logger.error("srs_write_telemetry collector_unavailable")
            else:
                logger.info("srs_write_telemetry collector_ready")
    return _store


def _gap() -> None:
    global _failures
    _failures += 1
    # Static text only: exception messages can contain paths, SQL and identifiers.
    logger.warning("srs_write_telemetry observation_missing count=%d", _failures)


def emit(**fields) -> None:
    try:
        store = get_store()
        if store is not None:
            store.put(**fields)
    except Exception:
        _gap()


def expire_observations() -> None:
    try:
        store = get_store()
        if store is not None:
            store.expire_if_due()
    except Exception:
        _gap()


def database_clock(db: Session) -> datetime:
    """Independent connection AFTER root completion, never an application clock.

    PostgreSQL clock_timestamp is wall time, including a commit/lock wait. The
    SQLite branch is only a test seam. This never opens a new ORM transaction.
    """
    bind = db.get_bind()
    if bind.dialect.name == "sqlite" and db.in_transaction():
        # A SQLite StaticPool can hand out the very same DBAPI connection. Do
        # not let closing a second facade roll back a frozen caller's writes.
        return datetime.fromisoformat(db.scalar(text(
            "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
        )))
    engine = getattr(bind, "engine", bind)
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            return connection.execute(text("SELECT clock_timestamp()")).scalar_one()
        return datetime.fromisoformat(connection.execute(text(
            "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
        )).scalar_one())


def _root_before_commit(db):
    if not db.in_nested_transaction() and _STATE in db.info:
        db.info[_STATE]["commit_started"] = True


def _root_committed(db):
    if not db.in_nested_transaction() and _STATE in db.info:
        db.info[_STATE]["outcome"] = "committed"


def _root_rolled_back(db):
    if not db.in_nested_transaction() and _STATE in db.info:
        state = db.info[_STATE]
        # A lost commit acknowledgement is ambiguous even if rollback succeeds.
        state["outcome"] = "commit_unknown" if state["commit_started"] else "rolled_back"


def _transaction_ended(db, transaction):
    if transaction.parent is not None:
        return
    state = db.info.pop(_STATE, None)
    if state is None:
        return
    outcome = state["outcome"] or (
        "commit_unknown" if state["commit_started"] else "abandoned"
    )
    completion = (state, outcome)
    if "srs_deferred_completions" in db.info:
        db.info["srs_deferred_completions"].append(completion)
    else:
        _complete_observation(db, *completion)


def _complete_observation(db, state, outcome):
    try:
        completed = database_clock(db)
    except Exception:
        completed = None
        _gap()
    for record in state["records"].values():
        emit(**record, outcome=outcome, database_at=completed)


@contextmanager
def completion_after_timing(db):
    """Keep completion clock/spool I/O outside the worker's commit timer."""
    if not os.environ.get(ENV):
        yield
        return
    completions = db.info.setdefault("srs_deferred_completions", [])
    try:
        yield
    finally:
        db.info.pop("srs_deferred_completions", None)
        for state, outcome in completions:
            _complete_observation(db, state, outcome)


def observe_session(db: Session, session, *, source: str | None = None) -> None:
    """Register a would-have-written transaction before the writer's first mutation.

    Registration is inert unless enabled. A pending row survives a process death,
    exposing a gap. Savepoint-local callers are conservatively unqualified; the
    production writers currently use root transactions only.
    """
    if get_store() is None:
        return
    try:
        source = source or db.info.get("srs_repair_source", "direct")
        sources = db.info.get("srs_upload_sources", {source})
        external = isinstance(db.get_bind(), Connection)
        if db.in_nested_transaction() or external:
            emit(observation_id=str(uuid.uuid4()), session_id=session.id,
                 started_at=session.started_at, sources=sources,
                 outcome="unsupported_external_transaction" if external else "unsupported_savepoint")
            return
        if not db.info.get("srs_observers_installed"):
            event.listen(db, "before_commit", _root_before_commit)
            event.listen(db, "after_commit", _root_committed)
            event.listen(db, "after_rollback", _root_rolled_back)
            event.listen(db, "after_transaction_end", _transaction_ended)
            db.info["srs_observers_installed"] = True
        state = db.info.setdefault(_STATE, {
            "records": {}, "outcome": None, "commit_started": False,
        })
        key = str(session.id)
        if key in state["records"]:
            return
        record = dict(observation_id=str(uuid.uuid4()), session_id=session.id,
                      started_at=session.started_at, sources=sources, wrote=False,
                      expires_at=time.time() + TTL_SECONDS,
                      job_id=db.info.get("srs_job_id"))
        state["records"][key] = record
        emit(**record, outcome="pending")
    except Exception:
        _gap()


def evidence_mutated(db: Session, session_id) -> None:
    if db.in_nested_transaction():
        return
    state = db.info.get(_STATE)
    if state and str(session_id) in state["records"]:
        state["records"][str(session_id)]["wrote"] = True


def prepare_worker_observation(db: Session, session_id) -> None:
    """Persist crash coverage before entering the graph advisory-lock window."""
    if get_store() is None:
        return
    from app.models import GameSession
    session = db.get(GameSession, session_id)
    if session is not None:
        observe_session(db, session)


def upload_sources(request) -> set[str]:
    sources = {"upload_final" if request.terminal_action is not None else "upload_ordinary"}
    if "recompute_opportunity" not in request.model_fields_set:
        sources.add("upload_legacy")
    if request.line_revision and request.recompute_opportunity:
        # A revised line includes revert uploads and subsequent uploads of that
        # line. The protocol cannot distinguish those, so do not invent precision.
        sources.add("upload_revised_line")
    return sources


def worker_signal(db: Session, session_id, outcome: str) -> None:
    if not os.environ.get(ENV):
        return
    emit(observation_id=str(uuid.uuid4()), session_id=session_id,
         sources=db.info.get("srs_upload_sources", {"worker"}), outcome=outcome,
         job_id=db.info.get("srs_job_id"))


def frozen_attempt(db: Session, session) -> None:
    """Retention-state integration seam: call BEFORE returning a frozen skip.

    Does not implement or select a freeze policy. Records the uncensored late
    attempt even when no evidence transaction can be committed.
    """
    if not os.environ.get(ENV):
        return
    try:
        now = database_clock(db)
    except Exception:
        now = None
        _gap()
    emit(observation_id=str(uuid.uuid4()), session_id=session.id,
         started_at=session.started_at, sources=db.info.get("srs_upload_sources", {
             db.info.get("srs_repair_source", "direct")}),
         outcome="frozen", database_at=now)


@contextmanager
def repair_source(db: Session, source: str):
    previous = db.info.get("srs_repair_source")
    db.info["srs_repair_source"] = source
    try:
        yield
    finally:
        if previous is None:
            db.info.pop("srs_repair_source", None)
        else:
            db.info["srs_repair_source"] = previous


def repair_operation(source: str, *, bulk_source: str | None = None):
    def decorate(function):
        @wraps(function)
        def wrapped(db, *args, **kwargs):
            # Manual repair processes need startup validation even for no-op scans.
            get_store()
            selected = db.info.get("srs_repair_source") or (
                bulk_source if bulk_source and kwargs.get("session_id") is None else source
            )
            with repair_source(db, selected):
                return function(db, *args, **kwargs)
        return wrapped
    return decorate
