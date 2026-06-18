"""Versioned registry of evidence contracts.

An *evidence contract* describes the data shape a cached analysis row satisfies,
independent of the search/engine profile that produced it. The comparator only
ever consumes a row's validated ``evidence_contract_id`` plus a
``contract_satisfied`` flag; the registry — not the comparator — knows which
contracts are compatible / supersets of one another.

Each contract carries its own semantic ``validate`` because field-presence alone
cannot prove usability (a non-null PV can still be unusable).
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import chess

from app.move_classification import VALID_CLASSIFICATIONS


def _is_finite_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_resolver_complete(data: dict) -> bool:
    """Mirror ``canResolveCachedAnalysis`` in src/workers/analysisUtils.ts.

    Requires a classification signal, a best move, and a multi-move best-line PV
    whose first move equals the best move.
    """
    has_classification = (
        data.get("classification") is not None or data.get("eval_delta") is not None
    )
    if not has_classification:
        return False
    best_move = data.get("best_move_uci")
    if not best_move:
        return False
    line = data.get("best_line_uci")
    if isinstance(line, str):
        line = line.split() if line else None
    return (
        isinstance(line, list)
        and len(line) > 1
        and line[0] == best_move
    )


def _validate_minimal_played_eval(data: dict) -> bool:
    value = data.get("played_eval")
    return _is_finite_int(value) or (
        isinstance(value, float) and math.isfinite(value)
    )


def _validate_minimal_best_eval(data: dict) -> bool:
    value = data.get("best_eval")
    return _is_finite_int(value) or (
        isinstance(value, float) and math.isfinite(value)
    )


def _pv_first_equals_best(data: dict) -> bool:
    best_move = data.get("best_move_uci")
    if not best_move:
        return False
    line = data.get("best_line_uci")
    if isinstance(line, str):
        line = line.split() if line else None
    return isinstance(line, list) and len(line) > 1 and line[0] == best_move


def _validate_resolver_complete_v2(data: dict) -> bool:
    """Stronger resolver-complete contract that fails closed.

    Requires the full eval triple every downstream consumer reads (drills,
    recording, SRS), an explicit enum-valid classification, a multi-move PV
    starting with the best move, AND internal delta consistency derived from the
    position's active color. A malformed/missing FEN returns ``False`` rather than
    propagating an exception through ``contract_satisfied``.
    """
    if not _pv_first_equals_best(data):
        return False

    classification = data.get("classification")
    if classification not in VALID_CLASSIFICATIONS:
        return False

    played_eval = data.get("played_eval")
    best_eval = data.get("best_eval")
    eval_delta = data.get("eval_delta")
    if not (_is_finite_int(played_eval) and _is_finite_int(best_eval)):
        return False
    if not (_is_finite_int(eval_delta) and eval_delta >= 0):
        return False

    fen = data.get("fen_before")
    if not isinstance(fen, str) or not fen:
        return False
    try:
        board = chess.Board(fen)
    except Exception:
        return False

    # played_eval/best_eval are white-relative cp; the stored delta is
    # side-to-move-relative and clamped at >= 0.
    if board.turn == chess.WHITE:
        expected = best_eval - played_eval
    else:
        expected = played_eval - best_eval
    expected = max(expected, 0)
    return eval_delta == expected


def _validate_position_complete(data: dict) -> bool:
    """Grain-specific contract for a stored position winner (position grain).

    Requires a best move, a multi-move best-line PV whose first move equals the
    best move, AND a usable position eval: either a finite CP ``best_eval`` or an
    explicit ``best_eval_mate``. Carries NO played-move delta — the position grain
    has no ``fen_before``/``played_eval``/``eval_delta`` and does no board
    construction (contrast ``_validate_resolver_complete_v2``).
    """
    if not _pv_first_equals_best(data):
        return False
    return _is_finite_int(data.get("best_eval")) or _is_finite_int(
        data.get("best_eval_mate")
    )


def _validate_move_complete(data: dict) -> bool:
    """Grain-specific contract for a per-move evidence row (move grain).

    Requires a usable played eval — finite CP ``played_eval`` OR explicit
    ``played_eval_mate`` — and an enum-valid ``classification``. Deliberately does
    NOT validate ``eval_delta == f(best_eval, played_eval)``: a move-only row has no
    ``best_eval``, which is exactly why post-split move rows cannot use
    resolver-complete-v2. Snapshot provenance ("eval-loss snapshot retained only if
    tied to the same canonical position winner") is a Phase-3 write-time concern,
    not validated here.
    """
    has_played = _is_finite_int(data.get("played_eval")) or _is_finite_int(
        data.get("played_eval_mate")
    )
    if not has_played:
        return False
    return data.get("classification") in VALID_CLASSIFICATIONS


@dataclass(frozen=True)
class Contract:
    contract_id: str
    required_fields: frozenset[str]
    # contract_ids this one is a registered superset/successor of.
    supersedes: frozenset[str]
    validate: Callable[[dict], bool]
    extra: dict = field(default_factory=dict)


RESOLVER_COMPLETE = "resolver-complete-v1"
RESOLVER_COMPLETE_V2 = "resolver-complete-v2"
MINIMAL_PLAYED_EVAL = "minimal-played-eval-v1"
MINIMAL_BEST_EVAL = "minimal-best-eval-v1"
# Grain-specific contracts introduced for g-position-analysis. They are
# independent of the resolver family (``supersedes=frozenset()``): legacy v2
# canonical rows are projected into each grain by the helpers below, NOT by
# registry supersession. resolver-complete-v2 stays the LEGACY read/projection
# contract and is intentionally left registered/unchanged so existing canonical
# rows stay trusted during the migration.
POSITION_COMPLETE = "position-complete-v1"
MOVE_COMPLETE = "move-complete-v1"

_CONTRACTS: dict[str, Contract] = {
    RESOLVER_COMPLETE_V2: Contract(
        contract_id=RESOLVER_COMPLETE_V2,
        required_fields=frozenset(
            {
                "fen_before",
                "best_move_uci",
                "best_line_uci",
                "classification",
                "played_eval",
                "best_eval",
                "eval_delta",
            }
        ),
        # A v2 canonical row may upgrade a v1 row or either minimal row; the
        # reverse is deliberately NOT allowed (v1 does not supersede v2).
        supersedes=frozenset(
            {RESOLVER_COMPLETE, MINIMAL_PLAYED_EVAL, MINIMAL_BEST_EVAL}
        ),
        validate=_validate_resolver_complete_v2,
    ),
    RESOLVER_COMPLETE: Contract(
        contract_id=RESOLVER_COMPLETE,
        required_fields=frozenset({"best_move_uci", "best_line_uci"}),
        # resolver-complete is a strict successor of the minimal eval-only
        # contracts: a canonical resolver-complete row may upgrade a row that
        # carried only an eval (so the canonical-upgrade requirement holds). The
        # minimal contracts deliberately do NOT supersede resolver-complete, so a
        # sparse row can never replace a complete one.
        supersedes=frozenset({MINIMAL_PLAYED_EVAL, MINIMAL_BEST_EVAL}),
        validate=_validate_resolver_complete,
    ),
    MINIMAL_PLAYED_EVAL: Contract(
        contract_id=MINIMAL_PLAYED_EVAL,
        required_fields=frozenset({"played_eval"}),
        supersedes=frozenset(),
        validate=_validate_minimal_played_eval,
    ),
    MINIMAL_BEST_EVAL: Contract(
        contract_id=MINIMAL_BEST_EVAL,
        required_fields=frozenset({"best_eval"}),
        supersedes=frozenset(),
        validate=_validate_minimal_best_eval,
    ),
    POSITION_COMPLETE: Contract(
        contract_id=POSITION_COMPLETE,
        # Both are unconditionally required. The best_eval/best_eval_mate
        # disjunction is enforced by ``validate``, NOT listed here:
        # ``required_fields`` is informational documentation only and must honestly
        # describe always-required fields (it is never consulted by
        # ``contract_satisfied`` — see test_evidence_contracts.py).
        required_fields=frozenset({"best_move_uci", "best_line_uci"}),
        supersedes=frozenset(),
        validate=_validate_position_complete,
    ),
    MOVE_COMPLETE: Contract(
        contract_id=MOVE_COMPLETE,
        # ``classification`` is the only unconditionally-required field; played_eval
        # is NOT always required because an explicit played-mate is an accepted
        # alternative. The played_eval-OR-played_eval_mate disjunction is enforced
        # by ``validate`` (``required_fields`` is informational only).
        required_fields=frozenset({"classification"}),
        supersedes=frozenset(),
        validate=_validate_move_complete,
    ),
}

# Contracts a browser game upload row is allowed to be classified as, most
# specific first.
BROWSER_ALLOWED_CONTRACTS = (
    RESOLVER_COMPLETE,
    MINIMAL_PLAYED_EVAL,
    MINIMAL_BEST_EVAL,
)


def get_contract(contract_id: str | None) -> Contract | None:
    if contract_id is None:
        return None
    return _CONTRACTS.get(contract_id)


def contract_satisfied(contract_id: str | None, data: dict) -> bool:
    """True when ``data`` passes the contract's semantic validation."""
    contract = get_contract(contract_id)
    if contract is None:
        return False
    return contract.validate(data)


def is_superset_or_successor(incoming_id: str | None, existing_id: str | None) -> bool:
    """True when ``incoming_id`` is the same contract as, or a registered
    superset/successor of, ``existing_id``."""
    if incoming_id is None or existing_id is None:
        return False
    if incoming_id == existing_id:
        return True
    contract = get_contract(incoming_id)
    if contract is None:
        return False
    return existing_id in contract.supersedes


def is_strict_successor(incoming_id: str | None, existing_id: str | None) -> bool:
    """True when ``incoming_id`` strictly succeeds ``existing_id`` (not equal)."""
    if incoming_id is None or existing_id is None or incoming_id == existing_id:
        return False
    return is_superset_or_successor(incoming_id, existing_id)


def select_browser_contract(data: dict) -> str | None:
    """Pick the most specific browser-allowed contract whose ``validate`` passes.

    Returns ``None`` when the row satisfies no allowed contract (caller rejects).
    """
    for contract_id in BROWSER_ALLOWED_CONTRACTS:
        if contract_satisfied(contract_id, data):
            return contract_id
    return None


def select_canonical_move_contract(data: dict) -> str | None:
    """Contract a post-split canonical MOVE-grain write must declare.

    Returns ``move-complete-v1`` when ``data`` satisfies the move-complete
    contract, else ``None`` (the caller drops the row rather than store evidence
    it does not satisfy — same convention as :func:`select_browser_contract`).

    It NEVER returns ``resolver-complete-v2``. After the position/move grain split
    a move row no longer carries the position facts (``best_eval`` etc.) that v2's
    cross-grain ``eval_delta == f(best_eval, played_eval)`` invariant validates, so
    a move-only row could not satisfy v2 anyway; and even a transitional row that
    still happens to carry stale position facts must not claim the cross-grain v2
    contract once position truth lives in ``position_analysis``. This is the
    write-side enforcement seam the canonical producer uses at the Phase 4 cutover
    (mirrors :func:`select_browser_contract` for the browser producer and
    :func:`app.position_analysis_repo.write_position_analysis_row` for positions).
    """
    if contract_satisfied(MOVE_COMPLETE, data):
        return MOVE_COMPLETE
    return None


# --- Legacy-v2 grain projection (library-only; unwired in Phase 1) -------------
#
# Let an existing authoritative ``analysis_cache`` resolver-complete-v2 row project
# into both grain-specific trust decisions during the transition, before backfill
# exists. Phase 1 only defines + unit-tests these; nothing imports them yet. When
# Phase 4/5 wires them, note that ``project_v2_to_move`` reads ``played_eval_mate``,
# which ``analysis.py._row_contract_data`` does not yet include — that projection
# must be extended then.


def project_v2_to_position(data: dict) -> dict:
    """Project a v2 cache-row dict into the position-grain contract shape."""
    return {
        "best_move_uci": data.get("best_move_uci"),
        "best_line_uci": data.get("best_line_uci"),
        "best_eval": data.get("best_eval"),
        "best_eval_mate": data.get("best_eval_mate"),
    }


def project_v2_to_move(data: dict) -> dict:
    """Project a v2 cache-row dict into the move-grain contract shape."""
    return {
        "played_eval": data.get("played_eval"),
        "played_eval_mate": data.get("played_eval_mate"),
        "classification": data.get("classification"),
        "eval_delta": data.get("eval_delta"),
    }


def _is_declared_v2(data: dict) -> bool:
    """True when the row DECLARES the resolver-complete-v2 contract.

    The gate is the declared ``evidence_contract_id`` only — NOT full v2
    re-validation. A v2 row is, by construction, both position-complete and
    move-complete, so re-running ``_validate_resolver_complete_v2`` here would make
    the grain projection below redundant and erase the grain distinction (e.g. a v2
    row with a bad classification must satisfy the position grain but NOT the move
    grain). The declared-id gate is what keeps these helpers honest to their name:
    a browser/minimal/v1 row that merely happens to carry the projected fields is
    not a legacy v2 projection and must fail closed.
    """
    return data.get("evidence_contract_id") == RESOLVER_COMPLETE_V2


def legacy_v2_satisfies_position(data: dict) -> bool:
    """True when a row DECLARED resolver-complete-v2, projected to the position
    grain, is position-complete. Fails closed for any non-v2 row."""
    if not _is_declared_v2(data):
        return False
    return _validate_position_complete(project_v2_to_position(data))


def legacy_v2_satisfies_move(data: dict) -> bool:
    """True when a row DECLARED resolver-complete-v2, projected to the move grain,
    is move-complete. Fails closed for any non-v2 row."""
    if not _is_declared_v2(data):
        return False
    return _validate_move_complete(project_v2_to_move(data))
