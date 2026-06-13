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


class Move(Base):
    __tablename__ = "moves"

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
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    fen_before: Mapped[str] = mapped_column(Text, nullable=False)
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
