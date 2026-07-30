from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.sql.functions import FunctionElement


class statement_timestamp(FunctionElement):  # noqa: N801 (renders as a SQL function)
    """Wall-clock time of the CURRENT STATEMENT, as a dialect-portable DDL default.

    Postgres' ``now()`` is TRANSACTION-start time, so a row inserted by a request
    that waited on a row lock would be stamped seconds before the insert actually
    happened. This renders ``statement_timestamp()`` on Postgres and falls back to
    ``CURRENT_TIMESTAMP`` elsewhere (SQLite has no ``statement_timestamp``), so the
    same metadata emits valid, equivalent DDL on both dialects.
    """

    type = DateTime(timezone=True)
    name = "statement_timestamp"
    inherit_cache = True


@compiles(statement_timestamp)
def _compile_statement_timestamp_default(element, compiler, **kw) -> str:
    return "CURRENT_TIMESTAMP"


@compiles(statement_timestamp, "postgresql")
def _compile_statement_timestamp_postgresql(element, compiler, **kw) -> str:
    return "statement_timestamp()"


def encode_uci_line(line: list[str] | None) -> str | None:
    """Serialize a UCI move list to the space-joined storage form."""
    if not line:
        return None
    return " ".join(line)


def decode_uci_line(value: str | None) -> list[str] | None:
    """Deserialize the space-joined storage form back to a UCI move list."""
    if not value:
        return None
    moves = value.split()
    return moves or None


class Base(DeclarativeBase):
    pass


BIGINT_SQLITE = BigInteger().with_variant(Integer, "sqlite")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    username: Mapped[str | None] = mapped_column(String(50))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("user_id", "fen_hash", name="uq_positions_user_fen_hash"),
        CheckConstraint("active_color in ('white','black')", name="ck_positions_active_color"),
        Index("idx_positions_user", "user_id"),
        Index("idx_positions_user_active_color", "user_id", "active_color"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False)
    fen_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fen_raw: Mapped[str] = mapped_column(Text, nullable=False)
    active_color: Mapped[str] = mapped_column(String(5), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Blunder(Base):
    __tablename__ = "blunders"
    __table_args__ = (
        UniqueConstraint("user_id", "position_id", name="uq_blunders_user_position"),
        Index("idx_blunders_user", "user_id"),
        Index("idx_blunders_position_user", "position_id", "user_id"),
        Index("idx_blunders_due", "user_id", "pass_streak", "last_reviewed_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False)
    position_id: Mapped[int] = mapped_column(BIGINT_SQLITE, ForeignKey("positions.id"), nullable=False)
    bad_move_san: Mapped[str] = mapped_column(String(10), nullable=False)
    best_move_san: Mapped[str] = mapped_column(String(10), nullable=False)
    eval_loss_cp: Mapped[int] = mapped_column(Integer, nullable=False)
    pass_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_reviewed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("game_sessions.id"), nullable=True)
    opening_family: Mapped[str | None] = mapped_column(Text)


class BlunderOpportunityEvent(Base):
    __tablename__ = "blunder_opportunity_events"
    __table_args__ = (
        UniqueConstraint("session_id", "blunder_id", name="uq_blunder_opportunity_session_blunder"),
        Index("idx_blunder_opportunity_blunder_time", "blunder_id", "occurred_at"),
        Index("idx_blunder_opportunity_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    blunder_id: Mapped[int] = mapped_column(BIGINT_SQLITE, ForeignKey("blunders.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("game_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurred_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    opportunity: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reached: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BlunderReview(Base):
    __tablename__ = "blunder_reviews"
    __table_args__ = (
        # reviewed_at is DESC to match the DDL in 20260208_01 (latest-review-first
        # lookup per blunder). Declared as an expression rather than a plain column
        # so autogenerate compares equal against the live index instead of emitting
        # a permanent spurious drop/create pair.
        Index("idx_blunder_reviews_blunder", "blunder_id", text("reviewed_at DESC")),
        # Partial unique index: the WHERE clause excludes NULL keys from
        # uniqueness, so two keyless reviews of the same blunder are both
        # permitted while a supplied key dedupes retries.
        Index(
            "uq_blunder_reviews_idempotency",
            "blunder_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    blunder_id: Mapped[int] = mapped_column(BIGINT_SQLITE, ForeignKey("blunders.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("game_sessions.id"), nullable=False)
    reviewed_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    move_played_san: Mapped[str] = mapped_column(String(10), nullable=False)
    eval_delta_cp: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # pass_streak value AFTER this review was applied. Captured so an idempotent
    # retry can reconstruct the exact original response even if later reviews have
    # since mutated the blunder. NULL on pre-migration rows.
    pass_streak_after: Mapped[int | None] = mapped_column(Integer, nullable=True)


class GameSession(Base):
    __tablename__ = "game_sessions"
    __table_args__ = (
        CheckConstraint("player_color in ('white','black')", name="ck_game_sessions_player_color"),
        CheckConstraint("session_mode in ('normal','drill')", name="ck_game_sessions_session_mode"),
        CheckConstraint(
            "drill_state is null or drill_state in ('active','root_reached','failed','abandoned','converted')",
            name="ck_game_sessions_drill_state",
        ),
        CheckConstraint(
            "drill_strictness is null or drill_strictness in ('lenient','standard','strict')",
            name="ck_game_sessions_drill_strictness",
        ),
        CheckConstraint(
            "drill_strictness_cp is null or (drill_strictness_cp >= 0 and drill_strictness_cp <= 50)",
            name="ck_game_sessions_drill_strictness_cp",
        ),
        CheckConstraint(
            "drill_terminal_reason is null or drill_terminal_reason in ('off_route','accuracy','natural_end')",
            name="ck_game_sessions_drill_terminal_reason",
        ),
        CheckConstraint(
            "((session_mode = 'normal' and drill_state is null) "
            "or (session_mode = 'drill' and drill_state is not null))",
            name="ck_game_sessions_mode_drill_state",
        ),
        CheckConstraint("rated_start_ply is null or rated_start_ply >= 0", name="ck_game_sessions_rated_start_ply"),
        CheckConstraint(
            "drill_root_reached_ply is null or drill_root_reached_ply >= 0",
            name="ck_game_sessions_drill_root_reached_ply",
        ),
        CheckConstraint(
            "drill_root_reached_ply is null or session_mode = 'drill'",
            name="ck_game_sessions_root_ply_requires_drill",
        ),
        CheckConstraint(
            "player_accuracy is null or (player_accuracy >= 0 and player_accuracy <= 100)",
            name="ck_game_sessions_player_accuracy",
        ),
        CheckConstraint(
            "derived_tail_rows is null or derived_tail_rows > 0",
            name="ck_game_sessions_derived_tail_rows",
        ),
        CheckConstraint(
            "session_mode = 'normal' "
            "or (drill_state = 'converted' and is_rated = true and normal_started_at is not null "
            "and converted_at is not null and rated_start_ply is not null) "
            "or (drill_state in ('active','root_reached','failed','abandoned') and is_rated = false and rated_start_ply is null)",
            name="ck_game_sessions_drill_rating_boundary",
        ),
        Index("idx_game_sessions_user", "user_id"),
        Index("idx_game_sessions_status", "status"),
        Index("idx_game_sessions_user_started", "user_id", "started_at"),
        Index("idx_game_sessions_user_mode_status", "user_id", "session_mode", "status"),
        Index("idx_game_sessions_drill_state", "drill_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False)
    started_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[str | None] = mapped_column(String(20))
    engine_elo: Mapped[int] = mapped_column(Integer, nullable=False)
    blunder_recorded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_rated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    player_color: Mapped[str] = mapped_column(String(5), nullable=False, server_default="white")
    pgn: Mapped[str | None] = mapped_column(Text)
    session_mode: Mapped[str] = mapped_column(String(10), nullable=False, server_default="normal")
    drill_state: Mapped[str | None] = mapped_column(String(12))
    drill_opening_key: Mapped[str | None] = mapped_column(Text)
    # Space-joined UCI line from the start position to the ad-hoc drill target
    # (encode_uci_line / decode_uci_line). NULL for registered-root drills.
    drill_line: Mapped[str | None] = mapped_column(Text)
    drill_strictness: Mapped[str | None] = mapped_column(String(12))
    drill_strictness_cp: Mapped[int | None] = mapped_column(Integer)
    drill_terminal_reason: Mapped[str | None] = mapped_column(String(20))
    normal_started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    converted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    rated_start_ply: Mapped[int | None] = mapped_column(Integer)
    # The drill's EVIDENCE BOUNDARY: the ply at which the opening root was CONFIRMED
    # reached (g-root-confirm-api). Write-once and never re-derived at runtime — the
    # opening graph is an input to FEN reconstruction, so a graph change could move or
    # erase a historical boundary and make the same session account differently at two
    # different times.
    #
    # Stamped ONLY by the drill route-check confirmation, which proves the arrival
    # against a server-recorded opponent decision (or, for a player-reached root, against
    # the decision two plies earlier) before writing. Serving the route move that WOULD
    # reach the root deliberately does not stamp it: a response lost after commit would
    # otherwise make a root no client ever applied durable.
    #
    # NULL means "no confirmed root", including for legacy 'root_reached' drills that
    # predate confirmation. That is a real, expected residue, not a defect: a drill with
    # no boundary contributes no reach evidence, while its targeted attempts survive
    # independently in opponent_decisions.
    #
    # NOT the same as rated_start_ply (the CONVERSION boundary) and NOT SessionMove.
    # segment — a drill can reach root, play on, and convert much later.
    drill_root_reached_ply: Mapped[int | None] = mapped_column(Integer)
    # Idempotency bookkeeping for the first-blunder recording decision. Plain
    # (non-FK) columns: recorded_blunder_id mirrors Blunder.id so retries can
    # echo back the recorded id; blunder_idempotency_key is the decision key.
    recorded_blunder_id: Mapped[int | None] = mapped_column(BIGINT_SQLITE, nullable=True)
    blunder_idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Per-session snapshot of the user's opening scores, as a JSON-encoded
    # {opening_key: opening_score} map (Text, JSON-as-string per the repo
    # convention), so end-of-session opening-score deltas have a stable "before" to
    # diff against (live games feed request_recompute incrementally, so the cached
    # score already reflects most of the game by the time it ends). Captured
    # ASYNCHRONOUSLY shortly after session start by the OpeningBaselineScheduler
    # worker (g-mxeo), and persisted ONLY when the pre-session cached batch is
    # provably fresh AND dated STRICTLY BEFORE started_at; otherwise it remains
    # NULL. NULL (older sessions, best-effort skip/race, or a dropped job) omits
    # the delta; "{}" means "captured, user had no scored openings yet" (every
    # crossed opening reads as new). See app/opening_score_delta.py.
    opening_score_baseline: Mapped[str | None] = mapped_column(Text)
    # Cached session accuracy. An integer 0..100 or NULL for sessions not yet
    # scored, guarded by the named range CHECK ck_game_sessions_player_accuracy.
    # player_accuracy_algo_version records which accuracy algorithm produced the
    # cached value so a future algo bump can invalidate/recompute selectively.
    # Release A's serving write hooks (g-accuracy-hooks) populate both on game
    # end and post-end move uploads for ended, VISIBLE sessions (normal games and
    # converted drills); active sessions and ended failed/abandoned drills stay
    # NULL. After Release B's read switch (g-b-cache-reads), /api/stats/summary
    # and /api/history READ player_accuracy through
    # app.accuracy.accuracy_for_sessions and compute nothing; those readers never
    # look at player_accuracy_algo_version, whose currency the backfill's
    # invalidation predicate owns. /api/session/{id}/analysis still computes live
    # through game_accuracy_for_rows. No read path ever WRITES either column.
    player_accuracy: Mapped[int | None] = mapped_column(Integer)
    player_accuracy_algo_version: Mapped[int | None] = mapped_column(SmallInteger)
    # Durable terminal-reconcile marker (g-short-move-rows): how many tail rows
    # were derived from the terminal PGN — at /end or by the historical repair —
    # because the client's final upload never committed them. NULL means the
    # reconcile never derived here (including every pre-feature session). It
    # records that derivation FIRED, not the provenance of the current rows: a
    # late full-history upsert may overwrite derived rows with the client's
    # richer record without clearing it. The fire-and-forget ``game_ended``
    # analytics props carry the same count but may drop; this column is what
    # makes recurrence measurable in the database itself. It is written by the
    # terminal transaction alongside status/pgn and is NOT part of
    # ``session_upload_receipt`` semantics — no receipt is ever derived.
    derived_tail_rows: Mapped[int | None] = mapped_column(Integer)


class Move(Base):
    __tablename__ = "moves"
    __table_args__ = (Index("idx_moves_to_position_id", "to_position_id"),)

    from_position_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("positions.id"),
        primary_key=True,
        nullable=False,
    )
    move_san: Mapped[str] = mapped_column(String(10), primary_key=True, nullable=False)
    to_position_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("positions.id"),
        nullable=False,
    )


class RatingHistory(Base):
    __tablename__ = "rating_history"
    __table_args__ = (
        Index("idx_rating_history_user_timestamp", "user_id", "recorded_at"),
        Index("uq_rating_history_game_session", "game_session_id", unique=True),
        # Durable-head index (Release A). Serves the "latest rated row for a user"
        # lookup ordered by games_played first: WHERE user_id = ? ORDER BY
        # games_played DESC, recorded_at DESC, id DESC LIMIT 1. The DESC ordering
        # on every trailing column lets Postgres satisfy that ORDER BY straight
        # from the index with no Sort node. The (user_id, recorded_at) index above
        # remains for chronological history reads.
        Index(
            "idx_rating_history_user_chain",
            "user_id",
            text("games_played DESC"),
            text("recorded_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, ForeignKey("users.id"), nullable=False)
    game_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("game_sessions.id"), nullable=False
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    is_provisional: Mapped[bool] = mapped_column(Boolean, nullable=False)
    games_played: Mapped[int] = mapped_column(Integer, nullable=False)
    chesscom_rating: Mapped[float | None] = mapped_column(Float)
    chesscom_rd: Mapped[float | None] = mapped_column(Float)
    lichess_rating: Mapped[float | None] = mapped_column(Float)
    lichess_rd: Mapped[float | None] = mapped_column(Float)
    lichess_volatility: Mapped[float | None] = mapped_column(Float)
    recorded_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SessionMove(Base):
    __tablename__ = "session_moves"
    __table_args__ = (
        CheckConstraint("color in ('white','black')", name="ck_session_moves_color"),
        CheckConstraint(
            "decision_source is null or decision_source in ('ghost_path','backend_engine','local_fallback')",
            name="ck_session_moves_decision_source",
        ),
        CheckConstraint("segment in ('drill','normal')", name="ck_session_moves_segment"),
        UniqueConstraint("session_id", "move_number", "color", name="uq_session_moves_session_move_color"),
        Index("idx_session_moves_session", "session_id"),
        Index("idx_session_moves_session_segment", "session_id", "segment"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("game_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    move_number: Mapped[int] = mapped_column(Integer, nullable=False)
    color: Mapped[str] = mapped_column(String(5), nullable=False)
    move_san: Mapped[str] = mapped_column(String(10), nullable=False)
    fen_after: Mapped[str] = mapped_column(Text, nullable=False)
    eval_cp: Mapped[int | None] = mapped_column(Integer)
    eval_mate: Mapped[int | None] = mapped_column(Integer)
    best_move_san: Mapped[str | None] = mapped_column(String(10))
    best_move_eval_cp: Mapped[int | None] = mapped_column(Integer)
    eval_delta: Mapped[int | None] = mapped_column(Integer)
    classification: Mapped[str | None] = mapped_column(String(20))
    fen_before: Mapped[str | None] = mapped_column(Text)
    best_move_uci: Mapped[str | None] = mapped_column(String(5))
    # Space-joined UCI moves of the root best-move principal variation.
    best_line_uci: Mapped[str | None] = mapped_column(Text)
    decision_source: Mapped[str | None] = mapped_column(String(20))
    target_blunder_id: Mapped[int | None] = mapped_column(BIGINT_SQLITE, ForeignKey("blunders.id"))
    segment: Mapped[str] = mapped_column(String(10), nullable=False, server_default="normal")
    # JSON-encoded browser-game-v2 DYNAMIC provenance for this move's own local
    # search (the seven self-reported engine/search values), or NULL for legacy,
    # cache-sourced, reconciled, malformed, or no-provenance rows (g-mk1d).
    #
    # Read at exactly one place: GET /{session_id}/analysis, to build the LIVE
    # operand for a REQUIRES_COMPARISON overlay decision — "is this stored
    # cross-user browser row stronger than what this session already computed?".
    # Never filtered, joined, or indexed, hence one opaque Text column rather than
    # seven typed ones. The FIXED identity half is deliberately NOT stored: it is
    # reconstructed from the server registry at read time so a hand-edited row
    # cannot claim an identity it did not earn.
    browser_provenance: Mapped[str | None] = mapped_column(Text)


class SessionUploadReceipt(Base):
    """Durable, transactional receipt of a final full move-history upload (g-upload-observe).

    Written inside the SAME transaction as the moves for a ``final_full`` upload
    (the end-of-session upload identified by a client-sent ``terminal_action``),
    keyed by the middleware-normalized ``client_request_id``. It is the join
    target that turns an observed client timeout into an exact commit
    classification: a receipt present ⇒ the final payload committed; a receipt
    absent (past the adjudication horizon) ⇒ the final request did NOT commit.
    Unlike the fire-and-forget ``session_moves_uploaded`` PostHog event, its
    presence/absence does not depend on analytics delivery — it is durable state.

    Append-only sink: ``session_id`` is a PLAIN column (NO FK) so the insert takes
    no ``KEY SHARE`` lock on ``game_sessions`` and cannot perturb the writer-lock
    DAG (SPEC.md). The insert is flushed BEFORE the ``evidence_seq`` cursor bump,
    so it never lands after the transaction's final blocking statement.
    ``client_request_id`` is NOT NULL: a ``final_full`` upload lacking a valid
    client id is rejected 400 before any writes, so a null-id receipt (which would
    falsely read as loss against the client's id) can never exist.
    """

    __tablename__ = "session_upload_receipt"
    __table_args__ = (
        Index("idx_session_upload_receipt_session", "session_id"),
        Index("idx_session_upload_receipt_user", "user_id"),
        Index("idx_session_upload_receipt_client_request_id", "client_request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False)
    # Middleware-normalized (canonical lowercase) client-generated id; the join key.
    client_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    # Middleware's server-generated request id, to cross-join api_request for
    # server-side timing. Text (not FK) — api_request lives in analytics, not the DB.
    server_request_id: Mapped[str | None] = mapped_column(Text)
    # The g-y90g finality flag as sent for this upload (final_full sends true).
    recompute_opportunity: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Server-side cross-check from game_session.session_mode ('normal'|'drill') for
    # MATCHED receipts only; the missing-receipt cohort is classified by the client
    # event's terminal_action instead (it has no receipt to read this from).
    session_mode: Mapped[str | None] = mapped_column(Text)
    # Client-sent, allowlisted terminal action that identified this as final_full.
    terminal_action: Mapped[str | None] = mapped_column(Text)
    # CLIENT-DECLARED Content-Length (validated int), named so it is never conflated
    # with the client event's payload_bytes (actual serialized bytes).
    content_length_bytes: Mapped[int | None] = mapped_column(Integer)
    # INSERT time (pre-commit, before the cursor bump), NOT commit-completion time.
    # Stamped APP-SIDE at flush rather than by ``now()``: Postgres defines now() as
    # TRANSACTION-start time, so a request that waited seconds on the session row
    # lock would be stamped seconds before its receipt was actually inserted — the
    # one thing this column must not do. The DDL default is the statement-clock
    # backstop for any non-ORM insert (never ``now()``, for the same reason), and
    # matches the migration on both dialects. Coarse adjudication ordering only;
    # server-commit TIMING comes from the joined api_request, not this column.
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=statement_timestamp(),
        nullable=False,
    )


class OpponentDecision(Base):
    """Authoritative, replayable record of one served opponent decision
    (g-ghost-target-server-record).

    Before this table the decision was computed, returned and forgotten: the only
    server-side trace was a fire-and-forget PostHog ``opponent_move_served``, and
    ``session_moves.target_blunder_id`` reached the database solely as a CLIENT echo
    (``SessionMoveInput``). That is unusable as the denominator of a targeted
    p_reach: a session that never uploads silently drops a FAILED steer, biasing the
    ratio upward, and a client-controlled denominator gates ghost eligibility.

    **Grain.** Opaque ``decision_id`` PK; ``UNIQUE (session_id, request_fingerprint)``
    is the replay key. ``(session_id, ply_before)`` is NOT a safe unique grain:
    ``rewindBoardLocally`` truncates history and the revert flow continues on the
    SAME session, and the endpoint validates only existence and ownership (never
    status), so a post-revert branch legitimately asks for a decision at the same ply
    from a different position and history. Replaying the old row could return a move
    that is illegal in the new branch, and a mismatched-FEN check on such a row would
    turn legitimate continuation into a conflict instead.

    **Envelope, not response.** ``served_at`` is not a ``NextOpponentMoveResponse``
    field, so this row cannot be an extracted copy of one. ``response_payload`` is the
    serialized response; ``target_blunder_id`` / ``resulting_fen`` /
    ``reaches_drill_root`` are extracted off it for indexing and validation;
    ``decision_id`` and ``served_at`` are envelope-level and stamped at construction.
    The payload is stored rather than reconstructed because UCI + SAN + resulting FEN
    cannot replay a response verbatim: ``target_blunder_id IS NULL`` cannot
    distinguish a pre-root route ghost move from an engine move (so ``mode`` and
    ``decision_source`` are unrecoverable), and ``target_blunder_srs`` snapshots
    counters that move between the original request and its retry.

    **FK to game_sessions, unlike SessionUploadReceipt.** That receipt keeps a plain
    FK-free ``session_id`` so it stays a pure sink alongside the ``evidence_seq``
    cursor bump (SPEC.md). ``next-opponent-move`` never writes the cursor, and
    ``session_moves`` / ``blunder_opportunity_events`` already carry FK-CASCADE to
    ``game_sessions`` from cursor-bumping paths. The route branch already holds
    ``FOR NO KEY UPDATE`` on this very row, so the insert's ``KEY SHARE`` is a
    same-transaction no-op; the ghost/engine branches hold no lock at all and take
    ``KEY SHARE`` on ``game_sessions`` then ``blunders`` — FK-check order matches
    ``record_blunder``'s session-then-blunder order, and ``KEY SHARE`` conflicts only
    with ``FOR UPDATE`` / key-column updates, of which this schema has none. The FK is
    load-bearing: retention is session-lifetime via ``ON DELETE CASCADE``, with no
    independent TTL (the 30-day p_reach window is a QUERY concern, not a storage one).

    ``target_blunder_id`` deliberately has no ``ondelete``, mirroring
    ``session_moves.target_blunder_id``: no blunder-delete path exists, ``SET NULL``
    would leave an extracted column disagreeing with its own ``response_payload``, and
    ``CASCADE`` would delete decision rows that root confirmation still needs.
    """

    __tablename__ = "opponent_decisions"
    __table_args__ = (
        # The replay key AND the concurrency arbiter: the normal ghost/engine path
        # holds no row lock (the pre-root branch rolls it back before falling
        # through), so two concurrent identical requests can both miss the lookup and
        # both compute. find_ghost_move is randomized, so their moves need not agree;
        # this constraint — not a lock — decides which one is served.
        UniqueConstraint(
            "session_id", "request_fingerprint", name="uq_opponent_decisions_session_fingerprint"
        ),
        # The targeted-counters aggregate (per-decision served_at cutoff + after-created
        # predicate, before any (session_id, target_blunder_id) grouping).
        Index("idx_opponent_decisions_target_served", "target_blunder_id", "served_at"),
        CheckConstraint("ply_before >= 0", name="ck_opponent_decisions_ply_before"),
        # No separate session_id index: the unique index's leading column already
        # answers "every decision for this session".
    )

    # Allocated APPLICATION-side (uuid4) and stamped into response_payload BEFORE the
    # insert, so the payload always carries the decision_id of the row it is stored
    # in. A server default is unknown until after the INSERT, which would force either
    # a post-insert payload rewrite or a payload disagreeing with its own row.
    decision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("game_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # sha256 over scheme version + NORMALIZED request FEN + the full UCI history.
    # History is part of the key because Maia consumes NextOpponentMoveRequest.moves
    # and two transpositions can share a FEN and a ply while being different inputs.
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    # app.fen.fen_hash(request.fen) — the FEN half on its own, queryable without
    # reparsing the fingerprint.
    request_fen_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # The full UCI history the client sent, as a canonical JSON array (JSON-as-string
    # per the repo convention). NOT the space-joined ``encode_uci_line`` form used by
    # ``drill_line``: that is lossy over what this endpoint accepts, since
    # ``NextOpponentMoveRequest.moves`` is ``list[str]`` with no per-element
    # validation — ``["e2e4 e7e5", "g1f3"]`` and ``["e2e4", "e7e5 g1f3"]`` would store
    # identical text AND share a ``ply_before``, so neither column could separate
    # them. NOT NULL: ``[]`` is a real empty history (legitimate at ply 0 for a
    # black-playing user), never an absent one.
    uci_history: Mapped[str] = mapped_column(Text, nullable=False)
    # len(request.moves). VALIDATED METADATA, not part of any key: root confirmation
    # checks current_ply == ply_before + 1.
    ply_before: Mapped[int] = mapped_column(Integer, nullable=False)
    # IS the targeted timeline — the 30-day cutoff and after-created predicate read
    # this column. blunder_opportunity_events stamps session.started_at instead, which
    # would assign a late-session decision the session's opening time and silently
    # drop targeting of any blunder created during that same session. Stamped app-side
    # at envelope construction; the DDL default is the statement clock, never now()
    # (transaction-start time would predate a lock wait).
    served_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=statement_timestamp(),
        nullable=False,
    )
    # The canonical serialized NextOpponentMoveResponse (Text, JSON-as-string per the
    # repo convention). Replay returns THIS, verbatim — no business logic, no re-query
    # of mutable state.
    response_payload: Mapped[str] = mapped_column(Text, nullable=False)
    target_blunder_id: Mapped[int | None] = mapped_column(
        BIGINT_SQLITE, ForeignKey("blunders.id")
    )
    # FEN after the served move. Route and ghost decisions always have one; the engine
    # branch derives it by applying the controller's UCI and stores NULL (with a
    # warning) if that move is not legal here, since nothing validates the controller
    # today. Engine decisions are never root decisions, so root confirmation's
    # resulting_fen check never reads a NULL.
    resulting_fen: Mapped[str | None] = mapped_column(Text)
    reaches_drill_root: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


class AnalysisCache(Base):
    __tablename__ = "analysis_cache"
    __table_args__ = (
        UniqueConstraint("fen_before", "move_uci", name="uq_analysis_cache_fen_move"),
        Index("idx_analysis_cache_fen", "fen_before"),
        Index("idx_analysis_cache_norm_move", "normalized_fen_before", "move_uci"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    fen_before: Mapped[str] = mapped_column(Text, nullable=False)
    # Normalized 4-field FEN of fen_before (see app.fen.normalize_fen). Derived from
    # the immutable key, indexed with move_uci so the opening tree can resolve an
    # eval via transposition fallback without scanning. NULL on rows whose FEN
    # failed to parse during backfill.
    normalized_fen_before: Mapped[str | None] = mapped_column(Text)
    move_uci: Mapped[str] = mapped_column(String(5), nullable=False)
    move_san: Mapped[str] = mapped_column(String(10), nullable=False)
    best_move_uci: Mapped[str | None] = mapped_column(String(5))
    best_move_san: Mapped[str | None] = mapped_column(String(10))
    # Space-joined UCI moves of the root best-move principal variation.
    best_line_uci: Mapped[str | None] = mapped_column(Text)
    played_eval: Mapped[int | None] = mapped_column(Integer)
    # White-relative mate count for the played move (NULL when not a mate).
    played_eval_mate: Mapped[int | None] = mapped_column(Integer)
    best_eval: Mapped[int | None] = mapped_column(Integer)
    # White-relative mate count for the best move (NULL when not a mate).
    best_eval_mate: Mapped[int | None] = mapped_column(Integer)
    eval_delta: Mapped[int | None] = mapped_column(Integer)
    classification: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="game")
    # Provenance / quality metadata (see app/analysis_profiles.py). NULL on
    # legacy rows, which are treated as untrusted/unidentified.
    analysis_profile_id: Mapped[str | None] = mapped_column(String(64))
    engine_name: Mapped[str | None] = mapped_column(String(64))
    engine_version: Mapped[str | None] = mapped_column(String(64))
    engine_build: Mapped[str | None] = mapped_column(String(128))
    network_id: Mapped[str | None] = mapped_column(String(128))
    search_limit_type: Mapped[str | None] = mapped_column(String(16))
    search_limit_value: Mapped[int | None] = mapped_column(Integer)
    threads: Mapped[int | None] = mapped_column(Integer)
    hash_mb: Mapped[int | None] = mapped_column(Integer)
    multipv: Mapped[int | None] = mapped_column(Integer)
    # Full NNUE network identities "<filename>:<hash>" (two embedded SF18 nets).
    eval_file_id: Mapped[str | None] = mapped_column(Text)
    eval_file_small_id: Mapped[str | None] = mapped_column(Text)
    # Version of the analyzer output contract that produced this row.
    analyzer_protocol_version: Mapped[str | None] = mapped_column(String(64))
    # Digest of the immutable analysis-identity bits of the producing profile.
    profile_manifest_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_contract_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnalysisCacheSubmission(Base):
    """(analysis_cache row, user) submitter-eligibility association (g-v21l).

    ONE row means exactly: *this user independently submitted a tuple consistent
    with this stored row*. It is NOT ownership, confers NO write rights, and is
    never exposed in an API response, log line, or metric dimension.

    Why an association and not a ``submitted_by_user_id`` column on
    ``analysis_cache``: a single column cannot express either case this bead must
    handle.

    * ``browser-analysis-multipv-v2`` is a FIXED profile
      (``dynamic_fields=frozenset()``), so ``_same_profile_strength_decision``
      returns ``None`` on its first branch and there is NO same-profile REPLACE
      path. Rows already stored by g-reuse-d21-search would migrate with a null
      owner and could never acquire one — an identical resubmission decides
      ``SAME_PROFILE_IDEMPOTENT`` and writes nothing. Every pre-existing key would
      be permanently dead for reuse. With an association, that same idempotent
      branch grants eligibility without touching an evidence column.
    * If users A and B independently submit the same tuple, one column records only
      one of them. First-wins denies B; ownership transfer denies A. Both are
      ordinary outcomes for a shared opening position.

    ``(analysis_cache_id, user_id)`` is the COMPOSITE PRIMARY KEY — the pair's
    uniqueness IS the table's identity — with no surrogate id. The separate
    reverse-order index serves the viewer-scoped read
    (``WHERE user_id = :viewer AND analysis_cache_id IN :ids``), whose leading
    column the primary key does not serve.

    Both FKs are ``ON DELETE CASCADE``: deleting an ``analysis_cache`` row drops
    its associations, and deleting a user cannot strand eligibility rows that a
    recycled id would inherit.

    The table is in :data:`EVIDENCE_EPOCH_SHARED_TABLES`, so association writes
    bump ``evidence_epoch`` through the same per-dialect triggers as evidence
    writes — associations are an input to the OPENING_EVIDENCE trust filter, so an
    association-only mutation must invalidate opening-score batches exactly as an
    evidence mutation does.
    """

    __tablename__ = "analysis_cache_submission"
    __table_args__ = (
        Index(
            "idx_analysis_cache_submission_user",
            "user_id",
            "analysis_cache_id",
        ),
    )

    analysis_cache_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("analysis_cache.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )


class PositionAnalysisRow(Base):
    """Single trusted position winner keyed by ``normalized_fen`` (position grain).

    DISTINCT from the ``PositionAnalysis`` pydantic model + ``position_analysis:
    dict[str, PositionAnalysis]`` session response field in ``app/api/session.py``.
    That map is full-``fen_before``-keyed *wire* grain (one entry per played
    position in a game); this is normalized-FEN-keyed *storage* grain (one winning
    analysis per canonical position). The two share a name on purpose to mark the
    boundary — a storage row must NEVER be returned as the session map directly; an
    adapter is required. See epic g-position-analysis.

    Column conventions mirror :class:`AnalysisCache`, but this table is keyed by the
    normalized FEN (not ``(fen_before, move_uci)``) and is replaced over time rather
    than append-only, so it carries ``updated_at``. No Phase-1 code writes this
    table; Phase 2 backfill and Phase 3 winner-replacement do.
    """

    __tablename__ = "position_analysis"
    __table_args__ = (
        UniqueConstraint("normalized_fen", name="uq_position_analysis_normalized_fen"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    # Canonical 4-field FEN (see app.fen.normalize_fen) — the single lookup key and
    # the column the uniqueness winner-per-position invariant is enforced on.
    normalized_fen: Mapped[str] = mapped_column(Text, nullable=False)
    # Representative full FEN of the winning run, kept for provenance/sampling only.
    # NEVER used for lookup or uniqueness (that is normalized_fen's job).
    fen: Mapped[str] = mapped_column(Text, nullable=False)
    # A stored winner without a best move is meaningless, so this is NOT NULL.
    best_move_uci: Mapped[str] = mapped_column(String(5), nullable=False)
    best_move_san: Mapped[str | None] = mapped_column(String(10))
    # Space-joined UCI moves of the best-move principal variation.
    best_line_uci: Mapped[str | None] = mapped_column(Text)
    best_eval: Mapped[int | None] = mapped_column(Integer)
    # White-relative mate count for the best move (NULL when not a mate). First-class
    # / unconditional: omitting it would silently lose forced-mate preference when
    # Phase 4 cuts the tree root/move sort keys over to this table.
    best_eval_mate: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="precomputed"
    )
    # Provenance / quality metadata (mirrors AnalysisCache; see app/analysis_profiles.py).
    analysis_profile_id: Mapped[str | None] = mapped_column(String(64))
    engine_name: Mapped[str | None] = mapped_column(String(64))
    engine_version: Mapped[str | None] = mapped_column(String(64))
    engine_build: Mapped[str | None] = mapped_column(String(128))
    network_id: Mapped[str | None] = mapped_column(String(128))
    search_limit_type: Mapped[str | None] = mapped_column(String(16))
    search_limit_value: Mapped[int | None] = mapped_column(Integer)
    threads: Mapped[int | None] = mapped_column(Integer)
    hash_mb: Mapped[int | None] = mapped_column(Integer)
    multipv: Mapped[int | None] = mapped_column(Integer)
    eval_file_id: Mapped[str | None] = mapped_column(Text)
    eval_file_small_id: Mapped[str | None] = mapped_column(Text)
    analyzer_protocol_version: Mapped[str | None] = mapped_column(String(64))
    profile_manifest_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_contract_id: Mapped[str | None] = mapped_column(String(64))
    # The analysis_cache.id the winner was projected/backfilled from (Phase 2 audit
    # trail). Plain nullable bigint, no FK, to keep backfill/delete ordering simple.
    source_cache_id: Mapped[int | None] = mapped_column(BIGINT_SQLITE)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # CONTRACT FOR PHASES 2/3: ``onupdate=func.now()`` fires only on ORM-flush
    # UPDATE and Core ``update()``. It does NOT fire on
    # ``insert().on_conflict_do_update()`` / bulk upserts — the likely
    # winner-replacement path. Any Core upsert/replacement MUST set ``updated_at``
    # explicitly in its conflict-update set (or add a DB trigger / server_onupdate)
    # so winner-replacement audit time is never silently stale.
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PositionAnalysisConflict(Base):
    """Append-only audit sink for backfill/recompute disagreements at a position.

    A real storage table (never logs/comments): when Phase 2 backfill sees
    candidate ``analysis_cache`` rows that disagree on the winner for a
    ``normalized_fen``, it records the candidates and the per-axis disagreement
    here. Many records may accrue per FEN across recomputes, so ``normalized_fen``
    is indexed but NOT unique and there is no ``updated_at``. Only Phase 2 writes
    this; Phase 1 just creates it.
    """

    __tablename__ = "position_analysis_conflicts"
    __table_args__ = (
        Index("idx_position_analysis_conflicts_norm", "normalized_fen"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    normalized_fen: Mapped[str] = mapped_column(Text, nullable=False)
    # The selected winner's position_analysis.id when one was chosen. Plain nullable
    # bigint, no FK, to keep backfill/delete ordering simple in both Postgres and
    # the FK-off SQLite test schema.
    position_analysis_id: Mapped[int | None] = mapped_column(BIGINT_SQLITE)
    # JSON array of disagreeing analysis_cache.id values.
    candidate_cache_ids: Mapped[str | None] = mapped_column(Text)
    # JSON array of {cache_id, source, profile, contract, best_move_uci,
    # best_line_uci, best_eval, best_eval_mate} candidate summaries.
    candidate_summaries: Mapped[str | None] = mapped_column(Text)
    # Per-axis disagreement: JSON array of the distinct candidate values for that
    # axis, NULL when the axis agreed. Separate columns (not one blob) so Phase 2
    # audits can filter by axis; mate disagreement is captured first-class.
    best_move_disagreement: Mapped[str | None] = mapped_column(Text)
    pv_disagreement: Mapped[str | None] = mapped_column(Text)
    best_eval_disagreement: Mapped[str | None] = mapped_column(Text)
    best_eval_mate_disagreement: Mapped[str | None] = mapped_column(Text)
    # e.g. selected_dominant, conflict_quarantine, conflict_best_known_kept.
    policy_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OpeningScoreBatch(Base):
    __tablename__ = "opening_score_batches"
    __table_args__ = (
        CheckConstraint("player_color in ('white','black')", name="ck_opening_score_batches_player_color"),
        UniqueConstraint("user_id", "player_color", "generation", name="uq_opening_score_batches_user_color_generation"),
        Index("idx_opening_score_batches_user_color", "user_id", "player_color", "generation"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False)
    player_color: Mapped[str] = mapped_column(String(5), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_fingerprint: Mapped[str | None] = mapped_column(Text)
    # Content fingerprint over the consumed evidence + registry/config; used to skip
    # recompute when scoring inputs are unchanged. NULL for pre-migration batches.
    inputs_fingerprint: Mapped[str | None] = mapped_column(Text)
    # Cheap freshness signal (g-jact), stamped at build time and SAMPLED BEFORE the
    # evidence read so each is a lower bound on the evidence in the batch:
    # - evidence_seq: the (user_id, player_color) OpeningScoreCursor.evidence_seq at
    #   build (per-user surfaces: session_moves / eligibility / blunders / reviews);
    # - cache_epoch: the global evidence_epoch value at build (shared surfaces:
    #   analysis_cache / position_analysis, advanced by DB triggers);
    # - scoped_shared_digest: hash over ONLY the shared digest lines (AC|/PA|/ACP|)
    #   at this batch's stored shared-FEN scope (opening_score_batch_shared_scope),
    #   so an epoch drift can be resolved without re-scanning session moves.
    # All NULL on pre-migration batches -> the cheap check reports stale -> rebuild.
    evidence_seq: Mapped[int | None] = mapped_column(BIGINT_SQLITE)
    cache_epoch: Mapped[int | None] = mapped_column(BIGINT_SQLITE)
    scoped_shared_digest: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class EvidenceEpoch(Base):
    """Single-row global change counter for the SHARED evidence tables (g-jact).

    ``value`` is advanced by MANDATORY database triggers (created in the alembic
    migration and mirrored in the conftest test schema — they are not part of ORM
    metadata) on every INSERT/UPDATE/DELETE against ``analysis_cache`` and
    ``position_analysis``, so any writer — repo txn, repair-script delete, backfill,
    future code — bumps it without app-level discipline. Only CHANGE matters, never
    magnitude: the cheap freshness check compares the live value to the one stamped
    on a batch at build time.

    The singleton row (id=1, value=0) MUST be seeded by the migration/test schema:
    the triggers do ``UPDATE ... WHERE id = 1``, which silently no-ops when the row
    is missing. A missing row degrades safely (freshness cannot be proven -> always
    rebuild) but forfeits the fast path.
    """

    __tablename__ = "evidence_epoch"
    __table_args__ = (CheckConstraint("id = 1", name="ck_evidence_epoch_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False, server_default="0")


# Tables whose writes must advance EvidenceEpoch (mirrored by the 20260708_01 and
# 20260727_01 migrations; keep the lists in sync).
#
# ``analysis_cache_submission`` (g-v21l) is here for the same reason the evidence
# tables are: submitter associations gate the OPENING_EVIDENCE trust filter, so an
# association-only write changes which rows a user may read even though every
# evidence column stays byte-identical. Without the bump the cheap freshness check
# would re-arm a batch computed when its user could not read evidence they can now
# read.
EVIDENCE_EPOCH_SHARED_TABLES = (
    "analysis_cache",
    "position_analysis",
    "analysis_cache_submission",
)


def ensure_evidence_epoch_infrastructure(bind) -> None:
    """Idempotently seed the ``evidence_epoch`` singleton and (re)create the
    shared-table triggers on an EXISTING schema.

    ``Base.metadata.create_all`` builds the tables but NOT the trigger DDL or
    the singleton row — without them ``capture_freshness_snapshot`` stamps a
    NULL ``cache_epoch`` and no batch can ever be proven fresh. Every non-alembic
    schema path (the E2E/dev seed script, the hand-written conftest test schema)
    must call this after creating tables. Alembic-managed databases get the same
    DDL from the 20260708_01 migration and do not need this.

    ``bind`` is an Engine or Connection; dialect-specific DDL is emitted for
    sqlite (row-level triggers) and postgresql (statement-level, including
    TRUNCATE — a maintenance truncate changes shared evidence like any delete).
    """
    from sqlalchemy import text as _text
    from sqlalchemy.engine import Connection as _Connection

    def _install(conn) -> None:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            conn.execute(_text(
                "INSERT INTO evidence_epoch (id, value) VALUES (1, 0)"
                " ON CONFLICT (id) DO NOTHING"
            ))
            conn.execute(_text(
                """
                CREATE OR REPLACE FUNCTION bump_evidence_epoch() RETURNS trigger AS $$
                BEGIN
                    UPDATE evidence_epoch SET value = value + 1 WHERE id = 1;
                    RETURN NULL;
                END;
                $$ LANGUAGE plpgsql
                """
            ))
            for table in EVIDENCE_EPOCH_SHARED_TABLES:
                conn.execute(_text(
                    f"DROP TRIGGER IF EXISTS trg_{table}_evidence_epoch ON {table}"
                ))
                conn.execute(_text(
                    f"""
                    CREATE TRIGGER trg_{table}_evidence_epoch
                    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON {table}
                    FOR EACH STATEMENT EXECUTE FUNCTION bump_evidence_epoch()
                    """
                ))
        else:
            # sqlite (and a best-effort generic fallback): row-level, per-event
            # (no multi-event or statement-level trigger syntax). N bumps per
            # batch write are harmless — only change matters, never magnitude.
            conn.execute(_text(
                "INSERT OR IGNORE INTO evidence_epoch (id, value) VALUES (1, 0)"
            ))
            for table in EVIDENCE_EPOCH_SHARED_TABLES:
                for event in ("INSERT", "UPDATE", "DELETE"):
                    conn.execute(_text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS trg_{table}_evidence_epoch_{event.lower()}
                        AFTER {event} ON {table}
                        BEGIN
                            UPDATE evidence_epoch SET value = value + 1 WHERE id = 1;
                        END
                        """
                    ))
        conn.commit()

    if isinstance(bind, _Connection):
        _install(bind)
    else:
        with bind.connect() as conn:
            _install(conn)


class OpeningScoreBatchSharedScope(Base):
    """Per-batch shared-FEN scope for the scoped shared freshness digest (g-jact).

    Captured at build time from the raw-input digest derivation itself (the BROAD
    candidate set — every eligible player-color move lacking a primary eval — NOT
    the overlay's narrower fallback candidates): ``kind='raw'`` rows hold the raw
    candidate ``fen_before`` set (exact ``analysis_cache`` move-grain lookups),
    ``kind='norm'`` rows the normalized-FEN set (``position_analysis`` + legacy
    ``analysis_cache`` position lookups). When the global ``evidence_epoch`` has
    drifted past the batch's stamp, freshness re-hashes only the shared digest
    lines over this stored scope instead of re-deriving it from session moves.

    Cascades on batch delete so retention pruning removes scope rows through the
    same generation-retention path as the other per-batch read models.
    """

    __tablename__ = "opening_score_batch_shared_scope"
    __table_args__ = (
        CheckConstraint("kind in ('raw','norm')", name="ck_opening_score_batch_shared_scope_kind"),
        Index("idx_opening_score_batch_shared_scope_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("opening_score_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    fen: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(4), nullable=False)


class OpeningPositionScore(Base):
    """Generation-scoped direct position-score read model for the opening tree.

    Sibling of :class:`UserOpeningScore`, persisted under the same
    ``opening_score_batches`` generation but keyed by ``(batch_id, normalized_fen)``
    instead of a named-root contract. Holds direct per-position metrics for the
    horizontal move tree so the tree read path never runs ``compute_root_score``
    once per visible card.

    Only rows the database actually needs are written (see
    ``opening_rootcalc.compute_position_scores``):

    - in-book positions with mastery evidence at/below the FEN (``has_evidence``);
    - connected observed off-book positions (``in_book`` is false), which may carry
      no-data metrics so the API can tell a navigable observed off-book node from
      an arbitrary unknown FEN.

    Static in-book positions with no evidence below are intentionally NOT
    materialized — they are already represented by ``OpeningGraph``; the API returns
    no-data for an in-graph FEN that is absent from the latest position batch. The
    four metric columns are nullable: ``has_evidence`` false means no-data (null
    score/confidence/coverage/weighted_depth, zero sample/game counts).

    ``batch_id`` cascades on delete from ``opening_score_batches`` exactly like
    ``user_opening_scores`` so retention pruning removes direct rows through the
    same generation-retention path.
    """

    __tablename__ = "opening_position_scores"
    __table_args__ = (
        CheckConstraint(
            "player_color in ('white','black')",
            name="ck_opening_position_scores_player_color",
        ),
        UniqueConstraint(
            "batch_id", "normalized_fen", name="uq_opening_position_scores_batch_fen"
        ),
        Index(
            "idx_opening_position_scores_batch_fen", "batch_id", "normalized_fen"
        ),
        Index(
            "idx_opening_position_scores_user_color", "user_id", "player_color"
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("opening_score_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False)
    player_color: Mapped[str] = mapped_column(String(5), nullable=False)
    # Normalized 4-field FEN — the position-score identity. Transpositions that
    # differ only in halfmove/fullmove clocks collapse to one row here.
    normalized_fen: Mapped[str] = mapped_column(Text, nullable=False)
    # True when the FEN is a reference OpeningGraph position. A persisted row with
    # in_book=False is a connected observed off-book node.
    in_book: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # True when mastery evidence exists at/below the FEN. False => no-data row:
    # the four metric columns are null and sample/game counts are zero.
    has_evidence: Mapped[bool] = mapped_column(Boolean, nullable=False)
    opening_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    coverage: Mapped[float | None] = mapped_column(Float)
    weighted_depth: Mapped[float | None] = mapped_column(Float)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Distinct games over the reachable subtree (see RootScore.game_count).
    game_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_practiced_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    computed_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class OpeningPositionEdge(Base):
    """Generation-scoped observed-edge read model for the opening tree.

    Sibling of :class:`OpeningPositionScore`, persisted under the same
    ``opening_score_batches`` generation but keyed by ``(batch_id, parent_fen,
    child_fen)`` — mirroring the ``EvidenceOverlay`` edge key. It materializes the
    observed move edges (structural shape plus the ``traversal_count`` /
    ``live_attempts`` / ``live_passes`` counters) the ``/api/openings/tree`` builder
    needs, so the tree read path no longer rebuilds ``overlay_evidence`` (a full
    session-history replay) on the request thread. The builder loads rows lazily by
    ``(batch_id, parent_fen)`` so a warm read costs only bounded per-parent indexed
    lookups for the visible line and its rendered frontier.

    ``quality_sum`` / ``quality_count`` are deliberately OMITTED: the tree never
    reads them, and the scorer builds its own in-memory overlay during recompute.
    Edges are reconstructed for the tree as ``EdgeEvidence(..., quality_sum=0.0,
    quality_count=0)``. If the scorer is ever changed to read scores from this table
    instead of its own overlay, the two quality columns must be added here.

    ``batch_id`` cascades on delete from ``opening_score_batches`` exactly like
    ``opening_position_scores`` so retention pruning removes edge rows through the
    same generation-retention path.
    """

    __tablename__ = "opening_position_edges"
    __table_args__ = (
        CheckConstraint(
            "player_color in ('white','black')",
            name="ck_opening_position_edges_player_color",
        ),
        UniqueConstraint(
            "batch_id", "parent_fen", "child_fen",
            name="uq_opening_position_edges_batch_parent_child",
        ),
        Index(
            "idx_opening_position_edges_batch_parent", "batch_id", "parent_fen"
        ),
        Index(
            "idx_opening_position_edges_user_color", "user_id", "player_color"
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("opening_score_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False)
    player_color: Mapped[str] = mapped_column(String(5), nullable=False)
    # Normalized 4-field FENs — the EvidenceOverlay edge identity. Transpositions
    # that differ only in clocks collapse to the same parent/child keys.
    parent_fen: Mapped[str] = mapped_column(Text, nullable=False)
    child_fen: Mapped[str] = mapped_column(Text, nullable=False)
    uci: Mapped[str] = mapped_column(Text, nullable=False)
    traversal_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    live_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    live_passes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # EdgeEvidence parity; not read by the tree.
    live_fails: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    computed_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class OpeningScoreCursor(Base):
    __tablename__ = "opening_score_cursors"
    __table_args__ = (
        CheckConstraint("player_color in ('white','black')", name="ck_opening_score_cursors_player_color"),
    )

    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True)
    player_color: Mapped[str] = mapped_column(String(5), primary_key=True)
    latest_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Per-(user,color) change counter over the PER-USER evidence surfaces the
    # opening-score digest consumes: session_moves content, game_sessions evidence
    # eligibility (SESSION_EVIDENCE_ELIGIBLE_SQL truth value), ghost-target blunders,
    # and blunder_reviews. Advanced by ``opening_cache.bump_evidence_seq`` at the
    # app-level choke-points, in the SAME transaction as the write it accounts for,
    # via an atomic in-DB column expression (never read-modify-write).
    #
    # OUT-OF-BAND-WRITER CONTRACT: any migration or maintenance script that mutates
    # session_moves, game_sessions (eligibility columns), blunders, or
    # blunder_reviews WITHOUT going through the app choke-points MUST either bump
    # OPENING_EVIDENCE_INPUTS_VERSION (globally invalidates every batch via the
    # registry fingerprint — the simplest safe hammer) or advance evidence_seq for
    # every affected (user_id, player_color) in the same migration. Otherwise a
    # stale batch is served as fresh FOREVER (the counter never advances).
    #
    # NOTE: this row is also upserted by ``reserve_opening_score_generation``; each
    # upsert's DO-UPDATE set_ must stay SINGLE-COLUMN (it owns latest_generation,
    # bump_evidence_seq owns evidence_seq) so neither clobbers the other.
    evidence_seq: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False, server_default="0")


class UserOpeningScore(Base):
    __tablename__ = "user_opening_scores"
    __table_args__ = (
        CheckConstraint("player_color in ('white','black')", name="ck_user_opening_scores_player_color"),
        UniqueConstraint("batch_id", "opening_key", name="uq_user_opening_scores_batch_opening"),
        Index("idx_user_opening_scores_batch", "batch_id"),
        Index("idx_user_opening_scores_user_color", "user_id", "player_color"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        BIGINT_SQLITE,
        ForeignKey("opening_score_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, nullable=False)
    player_color: Mapped[str] = mapped_column(String(5), nullable=False)
    opening_key: Mapped[str] = mapped_column(Text, nullable=False)
    opening_name: Mapped[str] = mapped_column(Text, nullable=False)
    opening_family: Mapped[str] = mapped_column(Text, nullable=False)
    opening_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    coverage: Mapped[float] = mapped_column(Float, nullable=False)
    weighted_depth: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # Distinct games over the opening's reachable subtree (see RootScore.game_count).
    # server_default backfills batches written before this column existed; they
    # repopulate on the next recompute (forced by the OPENING_EVIDENCE_INPUTS_VERSION bump).
    game_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_practiced_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    strongest_branch_name: Mapped[str | None] = mapped_column(Text)
    strongest_branch_key: Mapped[str | None] = mapped_column(Text)
    strongest_branch_score: Mapped[float | None] = mapped_column(Float)
    weakest_branch_name: Mapped[str | None] = mapped_column(Text)
    weakest_branch_key: Mapped[str | None] = mapped_column(Text)
    weakest_branch_score: Mapped[float | None] = mapped_column(Float)
    underexposed_branch_name: Mapped[str | None] = mapped_column(Text)
    underexposed_branch_key: Mapped[str | None] = mapped_column(Text)
    underexposed_branch_value: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
