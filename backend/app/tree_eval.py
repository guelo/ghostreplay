"""Opening-tree engine-eval lookups over the global ``analysis_cache``.

The horizontal opening tree (epic g-d5cu) shows an engine evaluation on every move
node. Evals come exclusively from ``analysis_cache`` (populated by opening-book
precompute and analyzed player moves) — the tree never launches its own engine and
never stores evals of its own.

A move node has the cache key ``(parent_fen, move_uci)``. The tree replays the
selected UCI line from the initial board, so ``parent_fen`` is a full six-field FEN
whose move clocks may differ from the stored ``fen_before`` (transpositions). Lookup
therefore tries the exact key first, then falls back to the indexed normalized
4-field FEN (``analysis_cache.normalized_fen_before`` + ``idx_analysis_cache_norm_move``).

All evals are stored and returned **white-relative** (positive favors White). Render
from a side's perspective with :func:`eval_for_perspective`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app.fen import normalize_fen
from app.models import AnalysisCache

# Deterministic source preference for the normalized fallback: a precomputed
# opening-book eval is preferred over a player-game eval, then any other source.
_SOURCE_RANK = {"precomputed": 0, "game": 1}
_OTHER_SOURCE_RANK = 2


@dataclass(frozen=True)
class MoveEval:
    """A white-relative engine eval. Exactly one of ``cp``/``mate`` is non-None."""

    cp: int | None
    mate: int | None


def _source_rank(source: str | None) -> int:
    return _SOURCE_RANK.get(source or "", _OTHER_SOURCE_RANK)


def _played_eval(row: AnalysisCache) -> MoveEval | None:
    """Eval of the position after the move (mate preferred over cp), or None."""
    if row.played_eval_mate is not None:
        return MoveEval(cp=None, mate=row.played_eval_mate)
    if row.played_eval is not None:
        return MoveEval(cp=row.played_eval, mate=None)
    return None


def _best_eval(row: AnalysisCache) -> MoveEval | None:
    """Eval of the position under the engine's best move (mate preferred), or None."""
    if row.best_eval_mate is not None:
        return MoveEval(cp=None, mate=row.best_eval_mate)
    if row.best_eval is not None:
        return MoveEval(cp=row.best_eval, mate=None)
    return None


def _move_sort_key(row: AnalysisCache) -> tuple:
    # Prefer rows with mate data, then precomputed > game > other, then lowest id.
    return (
        0 if row.played_eval_mate is not None else 1,
        _source_rank(row.source),
        row.id,
    )


def _root_sort_key(row: AnalysisCache) -> tuple:
    # Prefer mate data, then the canonical complete best-move row, then source, id.
    return (
        0 if row.best_eval_mate is not None else 1,
        0 if (row.best_move_uci is not None and row.move_uci == row.best_move_uci) else 1,
        _source_rank(row.source),
        row.id,
    )


def lookup_move_evals(
    session: Session,
    requests: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], MoveEval | None]:
    """Resolve a white-relative eval per ``(parent_fen, move_uci)`` request.

    Batched into at most two indexed queries regardless of node count: never scans
    ``analysis_cache`` and never queries once per child. Exact full-FEN+UCI match
    wins; otherwise the indexed normalized-FEN transposition fallback is used, with
    deterministic selection (mate data, then precomputed > game > other, then id).
    An entry is ``None`` only when no usable eval exists.
    """
    # Dedupe while preserving caller order; every request gets an entry.
    reqs = list(dict.fromkeys((f, u) for f, u in requests))
    result: dict[tuple[str, str], MoveEval | None] = {r: None for r in reqs}
    if not reqs:
        return result

    # Step 1: exact (fen_before, move_uci) hits.
    exact_rows = (
        session.query(AnalysisCache)
        .filter(tuple_(AnalysisCache.fen_before, AnalysisCache.move_uci).in_(reqs))
        .all()
    )
    exact_map = {(r.fen_before, r.move_uci): r for r in exact_rows}

    unresolved: list[tuple[str, str]] = []
    norm_of: dict[tuple[str, str], str] = {}
    for fen, uci in reqs:
        row = exact_map.get((fen, uci))
        if row is not None:
            ev = _played_eval(row)
            if ev is not None:
                result[(fen, uci)] = ev
                continue
        # Exact miss, or exact row carried no usable eval -> normalized fallback.
        try:
            norm_of[(fen, uci)] = normalize_fen(fen)
        except Exception:
            continue
        unresolved.append((fen, uci))

    if not unresolved:
        return result

    # Step 2: normalized (normalized_fen_before, move_uci) fallback.
    norm_pairs = list({(norm_of[k], k[1]) for k in unresolved})
    fallback_rows = (
        session.query(AnalysisCache)
        .filter(
            tuple_(
                AnalysisCache.normalized_fen_before, AnalysisCache.move_uci
            ).in_(norm_pairs)
        )
        .all()
    )
    by_norm_move: dict[tuple[str, str], list[AnalysisCache]] = {}
    for r in fallback_rows:
        if _played_eval(r) is None:
            continue
        by_norm_move.setdefault((r.normalized_fen_before, r.move_uci), []).append(r)

    for fen, uci in unresolved:
        candidates = by_norm_move.get((norm_of[(fen, uci)], uci))
        if candidates:
            result[(fen, uci)] = _played_eval(min(candidates, key=_move_sort_key))

    return result


def lookup_root_eval(session: Session, starting_fen: str) -> MoveEval | None:
    """White-relative eval for a column-0 root (the to-move position itself).

    Uses ``best_eval`` — the eval of the position under the engine's best move, which
    is a property of the position, not of any one row's played move. Any row at the
    starting FEN that carries a usable best eval qualifies; the canonical complete
    best-move row (``move_uci == best_move_uci``) is merely preferred in ranking.
    Exact FEN first, then normalized fallback. None when no usable best eval exists.
    """
    try:
        norm = normalize_fen(starting_fen)
    except Exception:
        norm = None

    rows = (
        session.query(AnalysisCache)
        .filter(AnalysisCache.fen_before == starting_fen)
        .all()
    )
    usable = [r for r in rows if _best_eval(r) is not None]

    if not usable and norm is not None:
        rows = (
            session.query(AnalysisCache)
            .filter(AnalysisCache.normalized_fen_before == norm)
            .all()
        )
        usable = [r for r in rows if _best_eval(r) is not None]

    if not usable:
        return None
    return _best_eval(min(usable, key=_root_sort_key))


def eval_for_perspective(
    ev: MoveEval | None, player_color: str
) -> MoveEval | None:
    """Convert a white-relative eval to the given side's perspective.

    White is unchanged; Black negates both cp and the mate sign (None stays None,
    cp 0 stays 0). Positive always means favorable for ``player_color``.
    """
    if ev is None:
        return None
    if player_color == "white":
        return ev
    if player_color != "black":
        raise ValueError(f"player_color must be 'white' or 'black', got {player_color!r}")
    return MoveEval(
        cp=None if ev.cp is None else -ev.cp,
        mate=None if ev.mate is None else -ev.mate,
    )
