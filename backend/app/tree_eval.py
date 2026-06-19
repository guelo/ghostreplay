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

All evals are stored and returned **white-relative** (positive favors White), and
the opening-tree cards render them as-is in that convention (+white / −black); the
per-column secondary sort applies the column's side-to-move favorability on the
backend (``openings._OpeningTreeBuilder._sort_key``), so nothing flips the displayed
sign. :func:`eval_for_perspective` flips a white-relative eval to a chosen side's
perspective for any caller that needs one (the tree no longer does).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app.analysis_trust import cache_row_as_move_dict, move_trust_flags, source_rank
from app.fen import normalize_fen
from app.models import AnalysisCache
from app.position_analysis_repo import TrustedPosition, resolve_trusted_position


@dataclass(frozen=True)
class MoveEval:
    """A white-relative engine eval. Exactly one of ``cp``/``mate`` is non-None."""

    cp: int | None
    mate: int | None


def _played_eval(row: AnalysisCache) -> MoveEval | None:
    """Eval of the position after the move (mate preferred over cp), or None."""
    if row.played_eval_mate is not None:
        return MoveEval(cp=None, mate=row.played_eval_mate)
    if row.played_eval is not None:
        return MoveEval(cp=row.played_eval, mate=None)
    return None


def _best_move_eval(tp: TrustedPosition) -> MoveEval | None:
    """White-relative position eval from a resolved trusted position (mate first)."""
    if tp.best_eval_mate is not None:
        return MoveEval(cp=None, mate=tp.best_eval_mate)
    if tp.best_eval is not None:
        return MoveEval(cp=tp.best_eval, mate=None)
    return None


def _move_trusted(row: AnalysisCache) -> bool:
    """True when the row's PLAYED-move evidence passes the move-grain trust gate."""
    return move_trust_flags(cache_row_as_move_dict(row))[2]


def _move_sort_key(row: AnalysisCache) -> tuple:
    # Ranks an already move-trusted candidate list (trust filtering precedes this).
    # Prefer rows with mate data, then precomputed > game > other, then lowest id.
    return (
        0 if row.played_eval_mate is not None else 1,
        source_rank(row.source),
        row.id,
    )


def lookup_move_evals(
    session: Session,
    requests: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], MoveEval | None]:
    """Resolve a white-relative eval per ``(parent_fen, move_uci)`` request.

    Batched into at most two indexed queries regardless of node count: never scans
    ``analysis_cache`` and never queries once per child. Exact full-FEN+UCI match
    wins; otherwise the indexed normalized-FEN transposition fallback is used. Both
    paths apply the move-grain trust gate FIRST — an untrusted browser/legacy row can
    never drive a played eval — then select deterministically among the trusted
    survivors (mate data, then precomputed > game > other, then id). An entry is
    ``None`` only when no usable trusted eval exists.
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
        if row is not None and _move_trusted(row):
            ev = _played_eval(row)
            if ev is not None:
                result[(fen, uci)] = ev
                continue
        # Exact miss, untrusted exact row, or no usable eval -> normalized fallback.
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
        # Drop untrusted rows BEFORE ranking so an untrusted eval can never win.
        if not _move_trusted(r):
            continue
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

    Position-grain evidence (``best_eval`` under the engine's best move) is resolved
    by :func:`app.position_analysis_repo.resolve_trusted_position`: the
    ``position_analysis`` storage winner, else a trusted legacy ``resolver-complete-v2``
    projection at the normalized FEN. An untrusted browser/legacy sibling can never
    surface here. The resolver ranks at the NORMALIZED grain (no exact-FEN-first
    preference), so a trusted mate/stronger row at a clock variant is not missed.
    ``None`` when no trusted position eval exists.
    """
    try:
        norm = normalize_fen(starting_fen)
    except Exception:
        return None

    tp = resolve_trusted_position(session, norm)
    if tp is None:
        return None
    return _best_move_eval(tp)


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
