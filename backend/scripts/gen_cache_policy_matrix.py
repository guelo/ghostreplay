"""Capture the storage-policy golden parity matrix (g-parity-matrix-evpolicy).

The matrix is every ``(existing row, incoming row)`` pair across a fixed set of
archetypes, plus the missing-key (insert) column, recorded as the
``(Decision, Reason)`` pair :func:`decide_analysis_cache_replacement` returns.
It exists to prove that the evidence-policy refactors — ``g-reuse-d21-search``
(comparator reroute, ``declared_profile_inactive`` gate, ``browser-analysis-v1``
retirement, ``browser-analysis-multipv-v2``), ``g-mk1d`` (``CacheRow.metadata``,
Rule 2a measured strength, comparator steps 4-5), and ``g-bgv1-cutover``
(``browser-game-v1`` retirement) — moved NOTHING except the cells that were
announced: a valid incoming row on a RETIRED profile, and nothing else.

This one file is the SINGLE source of truth for the archetype spec: the parity
test loads it by path so the goldens and the assertions can never describe two
different scenarios.

BOTH-TREE COMPATIBILITY. The same file is executed against the pre-refactor
baseline tree, so it must not touch anything that tree lacks:

  * only :class:`CacheRow` / :func:`decide_analysis_cache_replacement` are
    imported from ``app.analysis_cache_policy``; ``app.analysis_profiles`` is
    imported as a MODULE;
  * profile ids and contract ids are LITERAL strings — ``CANONICAL_LINUX_PROFILE_ID``
    and the two newer profile-id constants do not exist at the baseline;
  * archetype availability is decided with ``_ap.get_profile(pid) is not None``,
    never ``list_profiles()`` (added after the baseline);
  * ``stamp_dynamic_profile`` is resolved with ``getattr`` (g-mk1d only); the
    archetypes that call it are the HEAD-only ``browser-game-v2`` ones, and every
    archetype body is a LAMBDA so an unavailable archetype is skipped before its
    builder ever runs;
  * rows are built through :func:`row`, which filters kwargs by the running
    tree's ``dataclasses.fields(CacheRow)`` — so ``metadata`` is dropped at the
    baseline and populated at HEAD.

Each archetype is a RAW ROW DICT (the shape a writer actually persists);
``values`` / ``populated_fields`` / ``metadata`` are derived from it exactly as
``project_cache_row`` derives them, and the declared ``identity_verified`` /
``contract_satisfied`` booleans are asserted against the real projector by
``test_every_archetype_is_a_reachable_row``. The booleans are DECLARED rather
than projected here so the matrix stays a hermetic pin of the DECISION function
alone across both trees.

Usage (from backend/, venv active) — capture the CURRENT golden:

    PYTHONPATH=. python scripts/gen_cache_policy_matrix.py

Capture procedure for the BASELINE golden. The script is never copied into the
baseline worktree: the HEAD file is executed with ``PYTHONPATH`` pointed at the
baseline tree's ``backend``, so ``app.*`` resolves to baseline code while the
``__file__``-relative fixture path still resolves into the HEAD tree.
``sys.path[0]`` is ``backend/scripts``, which holds no ``app`` package, so there
is no shadowing. ``test_baseline_fixture_reproduces_from_pinned_commit`` runs
exactly this rather than trusting it:

    cd backend && source .venv/bin/activate
    WT=$(mktemp -d)/pre_refactor
    git worktree add --detach "$WT" be002bfa09ccc95562ea1cfbf9cdb3a0c048597c
    PYTHONPATH="$WT/backend" python scripts/gen_cache_policy_matrix.py \
      --out tests/fixtures/cache_policy_matrix_pre_refactor_be002bf.json
    git worktree remove "$WT"      # clean: nothing was written into it

FIXTURE POLICY. The baseline COMMIT is immutable — never re-pin it to a later
commit to make a test pass. A captured cell's value is immutable: a re-capture
may only ADD rows for new archetype ids or DROP rows for deleted ones, never
change the ``[decision, reason]`` of a retained key. An archetype's shape is
therefore immutable once captured — changing what a scenario means requires a
NEW archetype id. If a re-capture would change a retained cell, that is a
FINDING, not a refresh.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

import app.analysis_profiles as _ap
from app.analysis_cache_policy import CacheRow, decide_analysis_cache_replacement
from app.analysis_profiles import stamp_profile_full

# g-mk1d only. The archetypes that need it are HEAD-only, so a missing symbol at
# the baseline is fine — their profile is unregistered there and they are dropped
# before any builder runs.
_stamp_dynamic = getattr(_ap, "stamp_dynamic_profile", None)

BASELINE_COMMIT = "be002bfa09ccc95562ea1cfbf9cdb3a0c048597c"

CURRENT_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "cache_policy_matrix_current.json"
)

# Evidence fields, pinned as a literal rather than imported: the archetype rows
# must project identically in both trees, and
# ``test_every_archetype_is_a_reachable_row`` catches any drift from the module's
# own EVIDENCE_FIELDS by comparing against the real ``project_cache_row``.
EVIDENCE_FIELDS = (
    "best_move_uci",
    "best_move_san",
    "best_line_uci",
    "played_eval",
    "played_eval_mate",
    "best_eval",
    "best_eval_mate",
    "eval_delta",
    "classification",
)

# --- profile + contract ids (LITERALS: constants differ across the two trees) ---

CANONICAL = "canonical-sf18-depth24-v1"
CANONICAL_LINUX = "canonical-sf18-depth24-linux-v1"
BROWSER_GAME = "browser-game-v1"
BROWSER_GAME_V2 = "browser-game-v2"
BROWSER_ANALYSIS = "browser-analysis-v1"
MULTIPV = "browser-analysis-multipv-v2"
JEFFML = "jeffml-scores-v1"

RC_V1 = "resolver-complete-v1"
RC_V2 = "resolver-complete-v2"
MINIMAL_PLAYED_EVAL = "minimal-played-eval-v1"
MOVE_COMPLETE = "move-complete-v1"

# --- evidence shapes ------------------------------------------------------------
#
# One shared, semantically VALID base: white to move, so resolver-complete-v2's
# cross-field invariant is eval_delta == best_eval - played_eval, clamped at >= 0.
# Every derived shape stays reachable through ``project_cache_row`` — a shape that
# could not be persisted would pin an unreachable decision.

_START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

BASE = {
    "fen_before": _START_FEN,
    "best_move_uci": "e2e4",
    "best_move_san": "e4",
    # Multi-move PV whose first move equals best_move_uci: BOTH resolver
    # validators require it, so best_line_uci may never be dropped by a variant.
    "best_line_uci": "e2e4 e7e5",
    "played_eval": 10,
    "best_eval": 30,
    "eval_delta": 20,
    "classification": "good",
}

# Optional engine mate annotations (Rule 5 strips these before its completeness
# comparison; Rule 2 same-profile merge does not).
MATE = {**BASE, "played_eval_mate": 3, "best_eval_mate": 2}

# Completeness veto that is NOT a mate field: one fewer populated evidence field
# while staying contract-satisfied.
NO_SAN = {**BASE, "best_move_san": None}

# Same-profile merge conflict: still internally coherent (delta 70 == 30 - -40)
# yet disagreeing with BASE on the overlapping played_eval.
CONFLICT = {**BASE, "played_eval": -40, "eval_delta": 70}

# Contaminated row: identity still verifies, but the stored delta contradicts the
# evals, so resolver-complete-v2 validation fails.
INVALID = {**BASE, "eval_delta": 999}

SPARSE = {"played_eval": 10}

MOVE_ONLY = {"played_eval": 10, "classification": "good"}

# --- declared-dynamic provenance (browser-game-v2, HEAD only) -------------------

_BUILD = "a8fbc05ec6920b56d7485826dcb02c5ffd2826bcbf751cf973046f237a9096f1"
_NET = (
    "nn-9067e33176e8.nnue:"
    "9067e33176e8c5edb7aa8db6a3aedd012f84a1f39872e86357c6c2d0993f314d"
)
# A DIFFERENT (well-formed) network: rows carrying it are semantically
# incomparable to _NET rows however deep they searched.
_OTHER_NET = (
    "nn-4b1c07e5f2a9.nnue:"
    "4b1c07e5f2a9c0d3e6f80912345678909876543210abcdefabcdefabcdef0123"
)


def provenance(depth=17, **overrides):
    """A valid client-reported declared-dynamic identity at ``depth``."""
    return {
        "engine_version": "18",
        "engine_build": _BUILD,
        "eval_file_id": _NET,
        "search_limit_type": "depth",
        "search_limit_value": depth,
        "threads": 1,
        "hash_mb": 128,
        **overrides,
    }


# --- raw-row builders -----------------------------------------------------------


def _fixed(profile_id, contract_id, shape):
    """A row written by a FIXED-identity profile: all 12 identity columns stamped."""
    return {
        "analysis_profile_id": profile_id,
        "evidence_contract_id": contract_id,
        **shape,
        **stamp_profile_full(profile_id),
    }


def _dynamic(contract_id, shape, prov):
    """A row written by the DECLARED-DYNAMIC browser-game-v2 producer (g-mk1d)."""
    return {
        "analysis_profile_id": BROWSER_GAME_V2,
        "evidence_contract_id": contract_id,
        **shape,
        **_stamp_dynamic(BROWSER_GAME_V2, prov),
    }


def _legacy(contract_id, shape):
    """A profile-less legacy row: no identity columns at all."""
    return {
        "analysis_profile_id": None,
        "evidence_contract_id": contract_id,
        **shape,
    }


def row(data, *, identity_verified, contract_satisfied):
    """Project a raw row dict into a :class:`CacheRow` for the running tree.

    Mirrors ``project_cache_row`` field-for-field except that the two booleans are
    DECLARED by the archetype (so the matrix pins the decision function alone).
    Kwargs are filtered by the running tree's dataclass fields, which is what lets
    the same spec build rows in a tree whose ``CacheRow`` has no ``metadata``.
    """
    names = {f.name for f in dataclasses.fields(CacheRow)}
    kwargs = {
        "analysis_profile_id": data.get("analysis_profile_id"),
        "evidence_contract_id": data.get("evidence_contract_id"),
        "identity_verified": identity_verified,
        "contract_satisfied": contract_satisfied,
        "populated_fields": frozenset(
            f for f in EVIDENCE_FIELDS if data.get(f) is not None
        ),
        "values": {f: data.get(f) for f in EVIDENCE_FIELDS},
        "metadata": {f: data.get(f) for f in _ap.IDENTITY_FIELDS},
    }
    return CacheRow(**{k: v for k, v in kwargs.items() if k in names})


class Archetype:
    """One named row scenario. ``build`` is a lambda so an archetype whose profile
    is not registered in the running tree is skipped before it is ever built."""

    def __init__(self, id, profile_id, build, identity_verified, contract_satisfied):
        self.id = id
        self.profile_id = profile_id
        self._build = build
        self.identity_verified = identity_verified
        self.contract_satisfied = contract_satisfied
        self._data = None
        self._row = None

    @property
    def data(self) -> dict:
        if self._data is None:
            self._data = self._build()
        return self._data

    @property
    def row(self) -> CacheRow:
        if self._row is None:
            self._row = row(
                self.data,
                identity_verified=self.identity_verified,
                contract_satisfied=self.contract_satisfied,
            )
        return self._row


# The archetype spec. Order is documentation only — the serializer sorts by key.
ARCHETYPES = (
    # --- canonical (authoritative, active) --------------------------------------
    Archetype(
        "canonical_core_v2", CANONICAL,
        lambda: _fixed(CANONICAL, RC_V2, BASE), True, True,
    ),
    Archetype(
        "canonical_core_v2_mate", CANONICAL,
        lambda: _fixed(CANONICAL, RC_V2, MATE), True, True,
    ),
    Archetype(
        "canonical_core_v1", CANONICAL,
        lambda: _fixed(CANONICAL, RC_V1, BASE), True, True,
    ),
    Archetype(
        "canonical_no_san_v2", CANONICAL,
        lambda: _fixed(CANONICAL, RC_V2, NO_SAN), True, True,
    ),
    Archetype(
        "canonical_sparse", CANONICAL,
        lambda: _fixed(CANONICAL, MINIMAL_PLAYED_EVAL, SPARSE), True, True,
    ),
    Archetype(
        "canonical_move_complete", CANONICAL,
        lambda: _fixed(CANONICAL, MOVE_COMPLETE, MOVE_ONLY), True, True,
    ),
    Archetype(
        "canonical_conflict_v2", CANONICAL,
        lambda: _fixed(CANONICAL, RC_V2, CONFLICT), True, True,
    ),
    Archetype(
        "canonical_invalid", CANONICAL,
        lambda: _fixed(CANONICAL, RC_V2, INVALID), True, False,
    ),
    Archetype(
        "canonical_linux_core_v2", CANONICAL_LINUX,
        lambda: _fixed(CANONICAL_LINUX, RC_V2, BASE), True, True,
    ),
    # Canonical id whose stored build does NOT match the registry: effective legacy.
    Archetype(
        "unverified_canonical", CANONICAL,
        lambda: {**_fixed(CANONICAL, RC_V2, BASE), "engine_build": "deadbeef"},
        False, True,
    ),
    # --- browser-game-v1 (non-authoritative, not replacement-eligible) ----------
    Archetype(
        "browser_game_core_v1", BROWSER_GAME,
        lambda: _fixed(BROWSER_GAME, RC_V1, BASE), True, True,
    ),
    Archetype(
        "browser_game_core_v1_mate", BROWSER_GAME,
        lambda: _fixed(BROWSER_GAME, RC_V1, MATE), True, True,
    ),
    Archetype(
        "browser_game_sparse", BROWSER_GAME,
        lambda: _fixed(BROWSER_GAME, MINIMAL_PLAYED_EVAL, SPARSE), True, True,
    ),
    # --- browser-analysis-v1 (RETIRED at HEAD; active at the baseline) ----------
    Archetype(
        "browser_analysis_core_v2", BROWSER_ANALYSIS,
        lambda: _fixed(BROWSER_ANALYSIS, RC_V2, BASE), True, True,
    ),
    Archetype(
        "browser_analysis_core_v2_mate", BROWSER_ANALYSIS,
        lambda: _fixed(BROWSER_ANALYSIS, RC_V2, MATE), True, True,
    ),
    # Invalid AND retired: pins that the validity gate precedes the inactive gate.
    Archetype(
        "browser_analysis_invalid", BROWSER_ANALYSIS,
        lambda: _fixed(BROWSER_ANALYSIS, RC_V2, INVALID), True, False,
    ),
    # --- jeffml -----------------------------------------------------------------
    Archetype(
        "jeffml_sparse", JEFFML,
        lambda: _fixed(JEFFML, MINIMAL_PLAYED_EVAL, SPARSE), True, True,
    ),
    # --- legacy / profile-less ---------------------------------------------------
    Archetype(
        "legacy_uncontracted", None,
        lambda: _legacy(None, BASE), False, False,
    ),
    Archetype(
        "legacy_sparse_contracted", None,
        lambda: _legacy(MINIMAL_PLAYED_EVAL, SPARSE), False, True,
    ),
    # --- browser-analysis-multipv-v2 (HEAD only, g-reuse-d21-search) ------------
    Archetype(
        "multipv_core_v2", MULTIPV,
        lambda: _fixed(MULTIPV, RC_V2, BASE), True, True,
    ),
    Archetype(
        "multipv_core_v2_mate", MULTIPV,
        lambda: _fixed(MULTIPV, RC_V2, MATE), True, True,
    ),
    Archetype(
        "multipv_no_san_v2", MULTIPV,
        lambda: _fixed(MULTIPV, RC_V2, NO_SAN), True, True,
    ),
    Archetype(
        "multipv_conflict_v2", MULTIPV,
        lambda: _fixed(MULTIPV, RC_V2, CONFLICT), True, True,
    ),
    # --- browser-game-v2 (HEAD only, g-mk1d declared-dynamic) -------------------
    Archetype(
        "game_v2_d17", BROWSER_GAME_V2,
        lambda: _dynamic(RC_V1, BASE, provenance(17)), True, True,
    ),
    Archetype(
        "game_v2_d21", BROWSER_GAME_V2,
        lambda: _dynamic(RC_V1, BASE, provenance(21)), True, True,
    ),
    # Same depth as game_v2_d21 on a different device: EQUAL strength, different
    # provenance -> first wins, never a merge.
    Archetype(
        "game_v2_d21_hash64", BROWSER_GAME_V2,
        lambda: _dynamic(RC_V1, BASE, provenance(21, hash_mb=64)), True, True,
    ),
    # Different net: unrankable however deep either side searched.
    Archetype(
        "game_v2_d17_other_net", BROWSER_GAME_V2,
        lambda: _dynamic(RC_V1, BASE, provenance(17, eval_file_id=_OTHER_NET)),
        True, True,
    ),
    # Stronger search that would DROP an evidence field: the completeness guard
    # must veto the strength win.
    Archetype(
        "game_v2_d21_no_san", BROWSER_GAME_V2,
        lambda: _dynamic(RC_V1, NO_SAN, provenance(21)), True, True,
    ),
)

ARCHETYPE_IDS = tuple(a.id for a in ARCHETYPES)


def available_archetypes():
    """Archetypes whose profile is registered in the RUNNING tree.

    Uses ``get_profile`` rather than ``list_profiles`` — the latter does not exist
    in the pre-refactor baseline tree.
    """
    return tuple(
        a
        for a in ARCHETYPES
        if a.profile_id is None or _ap.get_profile(a.profile_id) is not None
    )


def build_matrix() -> dict:
    """Every ``(existing, incoming)`` decision, plus the missing-key column."""
    archetypes = available_archetypes()
    matrix: dict[str, list[str]] = {}
    for incoming in archetypes:
        decision, reason = decide_analysis_cache_replacement(None, incoming.row)
        matrix[f"None|{incoming.id}"] = [decision.value, reason.value]
        for existing in archetypes:
            decision, reason = decide_analysis_cache_replacement(
                existing.row, incoming.row
            )
            matrix[f"{existing.id}|{incoming.id}"] = [decision.value, reason.value]
    return matrix


def dumps_matrix(matrix: dict) -> str:
    """Serialize key-sorted, ONE CELL PER LINE, still valid JSON.

    ``json.dump(..., indent=N)`` puts every list element on its own line — four
    lines per cell and ~3.3k lines for the current matrix — which destroys the
    one-line-per-cell diff that makes a policy change reviewable. The output
    round-trips through ``json.loads``. Both fixtures AND the comparison tests go
    through this one function, so "byte-equal" has exactly one definition.
    """
    body = ",\n".join(
        f"  {json.dumps(key)}: {json.dumps(value)}"
        for key, value in sorted(matrix.items())
    )
    return "{\n" + body + "\n}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=CURRENT_FIXTURE,
        help="fixture path to write (default: the current-golden fixture)",
    )
    args = parser.parse_args()
    matrix = build_matrix()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dumps_matrix(matrix))
    print(f"Wrote {args.out} ({len(matrix)} cells)")


if __name__ == "__main__":
    main()
