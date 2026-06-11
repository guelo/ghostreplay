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


@dataclass(frozen=True)
class Contract:
    contract_id: str
    required_fields: frozenset[str]
    # contract_ids this one is a registered superset/successor of.
    supersedes: frozenset[str]
    validate: Callable[[dict], bool]
    extra: dict = field(default_factory=dict)


RESOLVER_COMPLETE = "resolver-complete-v1"
MINIMAL_PLAYED_EVAL = "minimal-played-eval-v1"
MINIMAL_BEST_EVAL = "minimal-best-eval-v1"

_CONTRACTS: dict[str, Contract] = {
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


def select_browser_contract(data: dict) -> str | None:
    """Pick the most specific browser-allowed contract whose ``validate`` passes.

    Returns ``None`` when the row satisfies no allowed contract (caller rejects).
    """
    for contract_id in BROWSER_ALLOWED_CONTRACTS:
        if contract_satisfied(contract_id, data):
            return contract_id
    return None
