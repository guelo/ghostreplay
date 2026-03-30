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
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


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


class BlunderReview(Base):
    __tablename__ = "blunder_reviews"
    __table_args__ = (
        Index("idx_blunder_reviews_blunder", "blunder_id", "reviewed_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    blunder_id: Mapped[int] = mapped_column(BIGINT_SQLITE, ForeignKey("blunders.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("game_sessions.id"), nullable=False)
    reviewed_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    move_played_san: Mapped[str] = mapped_column(String(10), nullable=False)
    eval_delta_cp: Mapped[int] = mapped_column(Integer, nullable=False)


class GameSession(Base):
    __tablename__ = "game_sessions"
    __table_args__ = (
        CheckConstraint("player_color in ('white','black')", name="ck_game_sessions_player_color"),
        Index("idx_game_sessions_user", "user_id"),
        Index("idx_game_sessions_status", "status"),
        Index("idx_game_sessions_user_started", "user_id", "started_at"),
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
    )

    id: Mapped[int] = mapped_column(BIGINT_SQLITE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BIGINT_SQLITE, ForeignKey("users.id"), nullable=False)
    game_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("game_sessions.id"), nullable=False
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    is_provisional: Mapped[bool] = mapped_column(Boolean, nullable=False)
    games_played: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SessionMove(Base):
    __tablename__ = "session_moves"
    __table_args__ = (
        CheckConstraint("color in ('white','black')", name="ck_session_moves_color"),
        UniqueConstraint("session_id", "move_number", "color", name="uq_session_moves_session_move_color"),
        Index("idx_session_moves_session", "session_id"),
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
    played_eval: Mapped[int | None] = mapped_column(Integer)
    best_eval: Mapped[int | None] = mapped_column(Integer)
    eval_delta: Mapped[int | None] = mapped_column(Integer)
    classification: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="game")
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
