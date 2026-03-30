from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import OpeningScoreBatch, OpeningScoreCursor, UserOpeningScore
from app.opening_evidence import EvidenceOverlay, overlay_evidence
from app.opening_graph import OpeningGraph, get_opening_graph
from app.opening_rootcalc import RootCalcConfig, RootScore, _Calculator
from app.opening_roots import OpeningRoots, get_opening_roots

PlayerColor = Literal["white", "black"]
_VALID_PLAYER_COLORS = {"white", "black"}


def _validate_player_color(player_color: str) -> None:
    if player_color not in _VALID_PLAYER_COLORS:
        raise ValueError(f"Unsupported player_color: {player_color}")


def _iter_named_roots(roots: OpeningRoots):
    for family_name in roots.get_families():
        for root in roots.get_family(family_name):
            yield root


def _calculator_has_evidence(calc: _Calculator, overlay: EvidenceOverlay) -> bool:
    domain_fens = set(calc.in_book_fens)
    domain_fens.update(calc.extension_fens.keys())
    if not domain_fens:
        return False
    if any(fen in overlay.nodes for fen in domain_fens):
        return True
    return any(parent_fen in domain_fens and child_fen in domain_fens for parent_fen, child_fen in overlay.edges)


def _build_cached_scores(
    player_color: PlayerColor,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    computed_at: datetime,
) -> list[RootScore]:
    config = RootCalcConfig()
    scores: list[RootScore] = []
    for root in _iter_named_roots(roots):
        calc = _Calculator(
            root.opening_key,
            player_color,
            graph,
            overlay,
            roots,
            config,
            computed_at,
            False,
        )
        if not _calculator_has_evidence(calc, overlay):
            continue
        scores.append(calc.compute())
    return scores


def get_latest_opening_score_batch(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> OpeningScoreBatch | None:
    _validate_player_color(player_color)
    return (
        db.query(OpeningScoreBatch)
        .filter(
            OpeningScoreBatch.user_id == user_id,
            OpeningScoreBatch.player_color == player_color,
        )
        .order_by(OpeningScoreBatch.generation.desc())
        .first()
    )


def reserve_opening_score_generation(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> int:
    _validate_player_color(player_color)
    dialect_name = db.bind.dialect.name if db.bind else ""

    if dialect_name == "sqlite":
        stmt = sqlite_insert(OpeningScoreCursor).values(
            user_id=user_id,
            player_color=player_color,
            latest_generation=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[OpeningScoreCursor.user_id, OpeningScoreCursor.player_color],
            set_={"latest_generation": OpeningScoreCursor.latest_generation + 1},
        ).returning(OpeningScoreCursor.latest_generation)
        generation = int(db.execute(stmt).scalar_one())
        db.commit()
        return generation

    if dialect_name == "postgresql":
        stmt = postgresql_insert(OpeningScoreCursor).values(
            user_id=user_id,
            player_color=player_color,
            latest_generation=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[OpeningScoreCursor.user_id, OpeningScoreCursor.player_color],
            set_={"latest_generation": OpeningScoreCursor.latest_generation + 1},
        ).returning(OpeningScoreCursor.latest_generation)
        generation = int(db.execute(stmt).scalar_one())
        db.commit()
        return generation

    cursor = (
        db.query(OpeningScoreCursor)
        .filter(
            OpeningScoreCursor.user_id == user_id,
            OpeningScoreCursor.player_color == player_color,
        )
        .first()
    )
    if cursor is None:
        cursor = OpeningScoreCursor(
            user_id=user_id,
            player_color=player_color,
            latest_generation=1,
        )
        db.add(cursor)
    else:
        cursor.latest_generation += 1

    db.commit()
    return cursor.latest_generation


def list_cached_opening_scores(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> tuple[OpeningScoreBatch | None, list[UserOpeningScore]]:
    batch = get_latest_opening_score_batch(db, user_id, player_color)
    if batch is None:
        return None, []
    rows = (
        db.query(UserOpeningScore)
        .filter(
            UserOpeningScore.batch_id == batch.id,
            UserOpeningScore.user_id == user_id,
            UserOpeningScore.player_color == player_color,
        )
        .order_by(
            UserOpeningScore.opening_family.asc(),
            UserOpeningScore.opening_name.asc(),
            UserOpeningScore.opening_key.asc(),
        )
        .all()
    )
    return batch, rows


def list_opening_score_candidate_pairs(
    db: Session,
    *,
    user_id: int | None = None,
    player_color: PlayerColor | None = None,
    limit: int | None = None,
) -> list[tuple[int, str]]:
    if player_color is not None:
        _validate_player_color(player_color)
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    sql = """
        SELECT pairs.user_id, pairs.player_color
        FROM (
            SELECT DISTINCT gs.user_id AS user_id, gs.player_color AS player_color
            FROM session_moves sm
            JOIN game_sessions gs ON gs.id = sm.session_id
            WHERE sm.fen_before IS NOT NULL

            UNION

            SELECT DISTINCT b.user_id AS user_id, p.active_color AS player_color
            FROM blunders b
            JOIN positions p ON p.id = b.position_id
            WHERE b.source_session_id IS NULL

            UNION

            SELECT DISTINCT b.user_id AS user_id, gs.player_color AS player_color
            FROM blunders b
            LEFT JOIN game_sessions gs ON gs.id = b.source_session_id
            WHERE gs.player_color IS NOT NULL
        ) pairs
        WHERE (:user_id IS NULL OR pairs.user_id = :user_id)
          AND (:player_color IS NULL OR pairs.player_color = :player_color)
        ORDER BY pairs.user_id ASC, pairs.player_color ASC
    """
    if limit is not None:
        sql += "\nLIMIT :limit"

    params = {"user_id": user_id, "player_color": player_color}
    if limit is not None:
        params["limit"] = limit

    rows = db.execute(text(sql), params).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


def has_opening_evidence(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> bool:
    return bool(
        list_opening_score_candidate_pairs(
            db,
            user_id=user_id,
            player_color=player_color,
            limit=1,
        )
    )


def recompute_opening_scores(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> OpeningScoreBatch:
    _validate_player_color(player_color)
    generation = reserve_opening_score_generation(db, user_id, player_color)
    computed_at = datetime.now(timezone.utc)
    graph = get_opening_graph()
    roots = get_opening_roots()
    overlay = overlay_evidence(db, user_id, player_color, graph)
    scores = _build_cached_scores(player_color, graph, overlay, roots, computed_at)

    batch = OpeningScoreBatch(
        user_id=user_id,
        player_color=player_color,
        generation=generation,
        computed_at=computed_at,
    )

    try:
        db.add(batch)
        db.flush()

        if scores:
            db.add_all(
                [
                    UserOpeningScore(
                        batch_id=batch.id,
                        user_id=user_id,
                        player_color=player_color,
                        opening_key=score.opening_key,
                        opening_name=score.opening_name,
                        opening_family=score.opening_family,
                        opening_score=score.opening_score,
                        confidence=score.confidence,
                        coverage=score.coverage,
                        weighted_depth=score.weighted_depth,
                        sample_size=score.sample_size,
                        last_practiced_at=score.last_practiced_at,
                        strongest_branch_name=(
                            score.strongest_branch.opening_name if score.strongest_branch else None
                        ),
                        strongest_branch_score=(
                            score.strongest_branch.value if score.strongest_branch else None
                        ),
                        weakest_branch_name=(
                            score.weakest_branch.opening_name if score.weakest_branch else None
                        ),
                        weakest_branch_score=score.weakest_branch.value if score.weakest_branch else None,
                        underexposed_branch_name=(
                            score.underexposed_branch.opening_name if score.underexposed_branch else None
                        ),
                        underexposed_branch_value=(
                            score.underexposed_branch.value if score.underexposed_branch else None
                        ),
                        computed_at=computed_at,
                    )
                    for score in scores
                ]
            )

        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(batch)
    return batch


def ensure_opening_scores(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> tuple[OpeningScoreBatch | None, list[UserOpeningScore]]:
    batch, rows = list_cached_opening_scores(db, user_id, player_color)
    if batch is not None:
        return batch, rows
    if not has_opening_evidence(db, user_id, player_color):
        return None, []
    recompute_opening_scores(db, user_id, player_color)
    return list_cached_opening_scores(db, user_id, player_color)


def recompute_opening_scores_if_needed(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> OpeningScoreBatch | None:
    batch = get_latest_opening_score_batch(db, user_id, player_color)
    if batch is None and not has_opening_evidence(db, user_id, player_color):
        return None
    return recompute_opening_scores(db, user_id, player_color)
