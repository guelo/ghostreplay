"""Durable final_full upload receipt on POST /moves (g-upload-observe).

The receipt is the exact commit-classification join target: written inside the SAME
transaction as the moves, ONLY for a final_full upload (identified by a client-sent
``terminal_action``), keyed by the middleware-normalized ``client_request_id``, and
flushed BEFORE the ``evidence_seq`` cursor bump. Its presence/absence — not the
fire-and-forget ``session_moves_uploaded`` event — is the measurement.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.models import SessionMove, SessionUploadReceipt
from sql_capture import capture_statements, cursor_last_before_commit

STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

CLIENT_ID = "7f3e4d2a-1b2c-4d5e-8f90-abcdef123456"


def _move(**overrides) -> dict:
    base = {
        "move_number": 1,
        "color": "white",
        "move_san": "e4",
        "fen_after": AFTER_E4_FEN,
        "eval_cp": 20,
        "best_move_san": "e4",
        "best_move_eval_cp": 20,
        "eval_delta": 0,
        "classification": "best",
        "fen_before": STARTING_FEN,
        "move_uci": "e2e4",
        "best_move_uci": "e2e4",
    }
    base.update(overrides)
    return base


def _post_moves(
    client,
    auth_headers,
    session_id,
    *,
    user_id: int,
    body_extra: dict | None = None,
    client_id: str | None = CLIENT_ID,
    moves: list | None = None,
):
    headers = auth_headers(user_id=user_id)
    if client_id is not None:
        headers["X-Client-Request-ID"] = client_id
    body = {"moves": [_move()] if moves is None else moves}
    if body_extra:
        body.update(body_extra)
    return client.post(
        f"/api/session/{session_id}/moves", json=body, headers=headers
    )


def _receipts(db_session, session_id):
    return (
        db_session.query(SessionUploadReceipt)
        .filter(SessionUploadReceipt.session_id == uuid.UUID(session_id))
        .all()
    )


# --- receipt written for final_full only -------------------------------------


def test_final_full_writes_a_receipt_keyed_by_client_id(
    client, auth_headers, create_game_session, db_session
):
    user_id = 5001
    session_id = create_game_session(user_id=user_id, player_color="white")

    resp = _post_moves(
        client,
        auth_headers,
        session_id,
        user_id=user_id,
        body_extra={"terminal_action": "game_end", "recompute_opportunity": True},
    )
    assert resp.status_code == 200, resp.text

    receipts = _receipts(db_session, session_id)
    assert len(receipts) == 1
    r = receipts[0]
    # Keyed by the (normalized) client id the client event also carries.
    assert str(r.client_request_id) == CLIENT_ID
    # server_request_id is the middleware id echoed on the response header.
    assert r.server_request_id == resp.headers.get("x-request-id")
    # session_mode is read from the session; terminal_action is the client's value.
    assert r.session_mode == "normal"
    assert r.terminal_action == "game_end"
    assert r.recompute_opportunity is True
    assert r.user_id == user_id


def test_final_full_normalizes_a_noncanonical_client_id(
    client, auth_headers, create_game_session, db_session
):
    user_id = 5002
    session_id = create_game_session(user_id=user_id, player_color="white")

    resp = _post_moves(
        client,
        auth_headers,
        session_id,
        user_id=user_id,
        body_extra={"terminal_action": "resign"},
        client_id=CLIENT_ID.upper(),  # uppercase -> normalized to canonical
    )
    assert resp.status_code == 200, resp.text

    receipts = _receipts(db_session, session_id)
    assert len(receipts) == 1
    assert str(receipts[0].client_request_id) == CLIENT_ID


@pytest.mark.parametrize("kind", ["incremental", "revert"])
def test_non_final_upload_writes_no_receipt(
    client, auth_headers, create_game_session, db_session, kind
):
    """An incremental/revert upload sends NO terminal_action, so it writes no
    receipt even with a client id header present (only final_full is recorded)."""
    user_id = 5003
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Neither sends terminal_action. revert would send recompute_opportunity=True;
    # that alone must NOT trigger a receipt (only terminal_action does).
    body_extra = {"recompute_opportunity": True} if kind == "revert" else {
        "recompute_opportunity": False
    }
    resp = _post_moves(
        client,
        auth_headers,
        session_id,
        user_id=user_id,
        body_extra=body_extra,
    )
    assert resp.status_code == 200, resp.text
    assert _receipts(db_session, session_id) == []


def test_empty_final_full_still_writes_a_receipt(
    client, auth_headers, create_game_session, db_session
):
    """A final_full upload with an empty move list writes no moves but MUST still
    commit a receipt. The join reads "200 with no receipt" as a noncommit, so a
    silent short-circuit here would manufacture false loss — the request may have
    spent its whole client deadline waiting on the session row lock and then
    succeeded."""
    user_id = 5009
    session_id = create_game_session(user_id=user_id, player_color="white")

    resp = _post_moves(
        client,
        auth_headers,
        session_id,
        user_id=user_id,
        moves=[],
        body_extra={"terminal_action": "game_end"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["moves_inserted"] == 0

    receipts = _receipts(db_session, session_id)
    assert len(receipts) == 1
    assert str(receipts[0].client_request_id) == CLIENT_ID
    assert receipts[0].terminal_action == "game_end"
    # ...and it is durable, not just staged in the request's session.
    db_session.expire_all()
    assert len(_receipts(db_session, session_id)) == 1


def test_empty_non_final_upload_writes_no_receipt(
    client, auth_headers, create_game_session, db_session
):
    user_id = 5010
    session_id = create_game_session(user_id=user_id, player_color="white")

    resp = _post_moves(
        client, auth_headers, session_id, user_id=user_id, moves=[]
    )
    assert resp.status_code == 200, resp.text
    assert _receipts(db_session, session_id) == []


def test_created_at_is_stamped_at_insert_not_transaction_start(
    client, auth_headers, create_game_session, db_session
):
    """created_at is stamped app-side at flush. Postgres' now() is TRANSACTION-
    start time, so a request that waited on the session row lock would be stamped
    seconds before its receipt was actually inserted; the app-side default keeps
    the column meaning what its name and the adjudication rules say it means."""
    user_id = 5011
    session_id = create_game_session(user_id=user_id, player_color="white")

    before = datetime.now(timezone.utc)
    resp = _post_moves(
        client,
        auth_headers,
        session_id,
        user_id=user_id,
        body_extra={"terminal_action": "game_end"},
    )
    assert resp.status_code == 200, resp.text
    after = datetime.now(timezone.utc)

    created_at = _receipts(db_session, session_id)[0].created_at
    # SQLite hands back a naive datetime; the value itself is UTC either way.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    assert before <= created_at <= after


def test_created_at_ddl_backstop_is_the_statement_clock_on_both_dialects():
    """The DDL default must be the STATEMENT clock (never Postgres' now(), which is
    transaction-start time) AND must be executable on SQLite, which has no
    statement_timestamp(). One shared construct renders both, so model metadata and
    the migration cannot drift apart."""
    column = SessionUploadReceipt.__table__.c.created_at
    default = column.server_default.arg

    assert (
        str(default.compile(dialect=postgresql.dialect())) == "statement_timestamp()"
    )
    assert str(default.compile(dialect=sqlite.dialect())) == "CURRENT_TIMESTAMP"

    sqlite_ddl = str(
        CreateTable(SessionUploadReceipt.__table__).compile(dialect=sqlite.dialect())
    )
    assert "CURRENT_TIMESTAMP" in sqlite_ddl
    assert "statement_timestamp" not in sqlite_ddl
    assert "now()" not in str(
        CreateTable(SessionUploadReceipt.__table__).compile(
            dialect=postgresql.dialect()
        )
    )


def test_created_at_backstop_executes_for_a_non_orm_insert():
    """The backstop is a real default, not just valid DDL.

    Built from the MODEL METADATA on a throwaway engine — not the shared test
    fixture, whose hand-written DDL already hardcodes CURRENT_TIMESTAMP and so
    would pass no matter what the model declares. An INSERT omitting created_at
    (no ORM default in play) must still stamp a value.
    """
    engine = create_engine("sqlite://")
    SessionUploadReceipt.__table__.create(engine)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO session_upload_receipt "
                    "(id, session_id, user_id, client_request_id, "
                    "recompute_opportunity) "
                    "VALUES (:id, :session_id, :user_id, :client_request_id, "
                    ":recompute)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "session_id": str(uuid.uuid4()),
                    "user_id": 5012,
                    "client_request_id": CLIENT_ID,
                    "recompute": True,
                },
            )
            assert (
                conn.execute(
                    text("SELECT created_at FROM session_upload_receipt")
                ).scalar()
                is not None
            )
    finally:
        engine.dispose()


def test_migration_ddl_default_matches_the_model_construct():
    """The revision keeps its OWN frozen copy of statement_timestamp (a migration
    must pin the DDL it shipped and never import mutable app code). This asserts the
    two independent constructs still compile identically, so the duplication cannot
    drift silently — the failure mode that made importing tempting."""
    spec = importlib.util.spec_from_file_location(
        "_migration_20260720_01",
        Path(__file__).parent
        / "alembic"
        / "versions"
        / "20260720_01_create_session_upload_receipt.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    model_default = SessionUploadReceipt.__table__.c.created_at.server_default.arg
    migration_default = migration.statement_timestamp()

    for dialect in (postgresql.dialect(), sqlite.dialect()):
        assert str(migration_default.compile(dialect=dialect)) == str(
            model_default.compile(dialect=dialect)
        )


# --- reject-before-write: no null-id receipt can exist -----------------------


@pytest.mark.parametrize("client_id", [None, "not-a-uuid"])
def test_final_full_without_valid_client_id_is_rejected_before_writes(
    client, auth_headers, create_game_session, db_session, client_id
):
    user_id = 5004
    session_id = create_game_session(user_id=user_id, player_color="white")

    resp = _post_moves(
        client,
        auth_headers,
        session_id,
        user_id=user_id,
        body_extra={"terminal_action": "game_end"},
        client_id=client_id,
    )
    assert resp.status_code == 400, resp.text

    # NEITHER a receipt NOR any session_moves row was written.
    assert _receipts(db_session, session_id) == []
    assert (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(session_id))
        .count()
        == 0
    )


# --- atomicity: the receipt shares the transaction ---------------------------


def test_receipt_is_rolled_back_with_a_failed_commit(
    client, auth_headers, create_game_session, db_session, monkeypatch
):
    user_id = 5005
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Force the cursor-bump path to fire, then make it raise AFTER the receipt has
    # been staged + flushed but BEFORE commit. Because the receipt shares the
    # transaction, the failure must leave no receipt row.
    monkeypatch.setattr("app.api.session.session_is_evidence_eligible", lambda s: True)

    def _boom(*a, **k):
        raise RuntimeError("cursor bump failed")

    monkeypatch.setattr("app.api.session.bump_evidence_seq", _boom)

    # The bare failure surfaces as a server error; TestClient re-raises it. The
    # request's DB session is still rolled back during dependency teardown, which
    # is what this test asserts.
    with pytest.raises(RuntimeError, match="cursor bump failed"):
        _post_moves(
            client,
            auth_headers,
            session_id,
            user_id=user_id,
            body_extra={"terminal_action": "game_end"},
        )

    db_session.expire_all()
    assert _receipts(db_session, session_id) == []
    # The moves rolled back too (fail-closed): the whole transaction is discarded.
    assert (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(session_id))
        .count()
        == 0
    )


# --- statement ordering: receipt INSERT precedes the cursor bump -------------


def test_receipt_insert_precedes_the_cursor_bump(
    client, auth_headers, create_game_session, db_session, monkeypatch
):
    """On the ON-CONFLICT path (SQLite/Postgres), the receipt INSERT is flushed
    ahead of the evidence-cursor bump — never after the transaction's final
    blocking statement. The generic row-by-row path stages the receipt at the
    identical point (after recompute, before the pre-bump flush)."""
    user_id = 5006
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Force exactly one cursor bump to order the receipt against; skip the
    # post-commit evidence pipeline so the request has a single commit + bump.
    monkeypatch.setattr("app.api.session.session_is_evidence_eligible", lambda s: True)
    monkeypatch.setattr(
        "app.api.session._should_run_session_move_evidence", lambda s: False
    )

    with capture_statements() as log:
        resp = _post_moves(
            client,
            auth_headers,
            session_id,
            user_id=user_id,
            body_extra={"terminal_action": "game_end"},
        )
    assert resp.status_code == 200, resp.text

    pre, cursor_idx = cursor_last_before_commit(log)
    receipt_idx = next(
        i
        for i, s in enumerate(pre)
        if s.lstrip().startswith("insert into session_upload_receipt")
    )
    assert receipt_idx < cursor_idx, pre


# --- PostHog independence + convenience-event enrichment ----------------------


def test_receipt_exists_even_when_posthog_delivery_drops(
    client, auth_headers, create_game_session, db_session, monkeypatch
):
    """The receipt is durable state, not the fire-and-forget event: a committed
    final_full upload is discoverable by client_request_id even when capture()
    delivers nothing (patched to a no-op that records nothing)."""
    user_id = 5007
    session_id = create_game_session(user_id=user_id, player_color="white")

    recorded: list = []
    monkeypatch.setattr("app.api.session.capture", lambda *a, **k: None)

    resp = _post_moves(
        client,
        auth_headers,
        session_id,
        user_id=user_id,
        body_extra={"terminal_action": "game_end"},
    )
    assert resp.status_code == 200, resp.text

    # No analytics were delivered...
    assert recorded == []
    # ...yet the durable receipt is present and joinable by client_request_id.
    match = (
        db_session.query(SessionUploadReceipt)
        .filter(SessionUploadReceipt.client_request_id == uuid.UUID(CLIENT_ID))
        .all()
    )
    assert len(match) == 1
    assert str(match[0].session_id) == session_id


def test_session_moves_uploaded_carries_client_id_mode_and_finality(
    client, auth_headers, create_game_session, db_session, monkeypatch
):
    user_id = 5008
    session_id = create_game_session(user_id=user_id, player_color="white")

    calls: list[tuple] = []
    monkeypatch.setattr("app.api.session.capture", lambda *a, **k: calls.append(a))

    resp = _post_moves(
        client,
        auth_headers,
        session_id,
        user_id=user_id,
        body_extra={"terminal_action": "game_end", "recompute_opportunity": True},
    )
    assert resp.status_code == 200, resp.text

    uploaded = [a for a in calls if len(a) >= 2 and a[1] == "session_moves_uploaded"]
    assert uploaded, "expected a session_moves_uploaded event"
    props = uploaded[0][2]
    assert props["client_request_id"] == CLIENT_ID
    assert props["recompute_opportunity"] is True
    assert props["session_mode"] == "normal"
