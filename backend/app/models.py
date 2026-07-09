from __future__ import annotations

import uuid
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


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
        Index("idx_blunder_reviews_blunder", "blunder_id", "reviewed_at"),
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


# Tables whose writes must advance EvidenceEpoch (mirrored by the 20260708_01
# migration; keep the two lists in sync).
EVIDENCE_EPOCH_SHARED_TABLES = ("analysis_cache", "position_analysis")


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
