"""Grain-specific read-time trust helpers (g-position-analysis Phase 4).

DB-free, dict-based trust decisions that tell a payload-producing consumer whether
a row's POSITION evidence (best move / PV / position eval) or MOVE evidence
(played eval / classification) may be trusted, independent of one another.

The two grains are gated by their OWN expected contract id, never by whatever
contract the row merely declares: an authoritative ``move-complete-v1`` row must
NOT read as position-trusted, and an authoritative ``position-complete-v1`` row
must NOT read as move-trusted. A legacy authoritative ``resolver-complete-v2``
``analysis_cache`` row projects into BOTH grains during the migration (the
projection helpers in :mod:`app.evidence_contracts` fail closed for any non-v2
row).

This module imports only :mod:`app.evidence_contracts` and
:mod:`app.analysis_profiles`, so it sits at the bottom of the dependency graph:
``analysis_trust`` ← ``position_analysis_policy`` / ``position_analysis_repo`` /
``tree_eval`` / ``api.analysis`` / ``api.session``, with no edges back. The
row→dict projectors read attributes via ``getattr`` so the module needs no ORM
import (and so works for both ``analysis_cache`` and ``position_analysis`` rows).
"""
from __future__ import annotations

from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import (
    MOVE_COMPLETE,
    POSITION_COMPLETE,
    contract_satisfied,
    legacy_v2_satisfies_move,
    legacy_v2_satisfies_position,
)
from app.evidence_policy import verify_identity

# Deterministic source preference shared by the position-grain ranking (the repo's
# legacy fallback) and the move-grain ranking (``tree_eval._move_sort_key``): a
# precomputed opening-book eval first, then stronger browser-analysis-board
# evidence, then a player-game eval, then any other or unknown source. Hosted here
# (a neutral module) so both rankings share one definition without an import cycle.
#
# ``analysis`` (g-cache-stronger-evals) ranks between precomputed and game and is
# stamped by rows the analysis-evidence endpoint writes. Its ONLY functional effect
# is in ``tree_eval.lookup_move_evals`` tier 4 (the normalized untrusted
# transposition fallback): a normalized ``analysis`` row outranks a normalized
# ``game`` row there ONLY when no exact untrusted row exists (tier 3 exact rows are
# checked first). Position-grain resolution is unaffected because browser-analysis
# is non-authoritative and ``resolve_trusted_positions`` pre-filters to trusted rows
# before sorting, so the ``analysis`` tier is inert for that consumer.
_SOURCE_RANK = {"precomputed": 0, "analysis": 1, "game": 2}
_OTHER_SOURCE_RANK = 3


def source_rank(source: str | None) -> int:
    return _SOURCE_RANK.get(source or "", _OTHER_SOURCE_RANK)


def _effectively_authoritative(data: dict) -> bool:
    """Profile is authoritative+active AND every IDENTITY_FIELDS column matches.

    The single definition shared by the position write policy
    (:mod:`app.position_analysis_policy`) and the read-time grain trust helpers
    below; mirrors ``api/analysis.py:_is_authoritative`` over a plain dict. Excludes
    browser-game / JeffML (non-authoritative) and any row whose stored identity does
    not back up its claimed profile.
    """
    profile = get_profile(data.get("analysis_profile_id"))
    if profile is None or not profile.authoritative or not profile.active:
        return False
    return verify_identity(data)


def position_trust_flags(data: dict) -> tuple[bool, bool, bool]:
    """``(authoritative, contract_satisfied, position_trusted)`` for a position row.

    ``contract_satisfied`` is gated on the position grain SPECIFICALLY: a native
    ``position-complete-v1`` row, OR a legacy ``resolver-complete-v2`` row whose
    position projection is complete. A row that declares another contract (e.g.
    ``move-complete-v1``) is not position-satisfied here even when authoritative.
    """
    authoritative = _effectively_authoritative(data)
    cid = data.get("evidence_contract_id")
    satisfied = (
        cid == POSITION_COMPLETE and contract_satisfied(POSITION_COMPLETE, data)
    ) or legacy_v2_satisfies_position(data)
    return authoritative, satisfied, (authoritative and satisfied)


def move_trust_flags(data: dict) -> tuple[bool, bool, bool]:
    """``(authoritative, contract_satisfied, move_trusted)`` for a move-grain row.

    ``contract_satisfied`` is gated on the move grain SPECIFICALLY: a native
    ``move-complete-v1`` row, OR a legacy ``resolver-complete-v2`` row whose move
    projection is complete. A native ``position-complete-v1`` row is not
    move-satisfied here even when authoritative.
    """
    authoritative = _effectively_authoritative(data)
    cid = data.get("evidence_contract_id")
    satisfied = (
        cid == MOVE_COMPLETE and contract_satisfied(MOVE_COMPLETE, data)
    ) or legacy_v2_satisfies_move(data)
    return authoritative, satisfied, (authoritative and satisfied)


# Identity columns both projectors copy so the trust helpers can verify identity.
_IDENTITY_PROJECTION = ("analysis_profile_id", "evidence_contract_id", *IDENTITY_FIELDS)


def cache_row_as_position_dict(row) -> dict:
    """Project an ``analysis_cache`` row into the position-grain trust dict.

    Reads attributes via ``getattr`` (no ORM import). ``best_line_uci`` stays in its
    space-joined storage form — the contract validators accept the string.
    """
    data = {f: getattr(row, f, None) for f in _IDENTITY_PROJECTION}
    data["best_move_uci"] = getattr(row, "best_move_uci", None)
    data["best_line_uci"] = getattr(row, "best_line_uci", None)
    data["best_eval"] = getattr(row, "best_eval", None)
    data["best_eval_mate"] = getattr(row, "best_eval_mate", None)
    return data


def cache_row_as_move_dict(row) -> dict:
    """Project an ``analysis_cache`` row into the move-grain trust dict."""
    data = {f: getattr(row, f, None) for f in _IDENTITY_PROJECTION}
    data["played_eval"] = getattr(row, "played_eval", None)
    data["played_eval_mate"] = getattr(row, "played_eval_mate", None)
    data["classification"] = getattr(row, "classification", None)
    data["eval_delta"] = getattr(row, "eval_delta", None)
    return data
