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

from dataclasses import dataclass

from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import (
    MOVE_COMPLETE,
    POSITION_COMPLETE,
    contract_satisfied,
    legacy_v2_satisfies_move,
    legacy_v2_satisfies_position,
)
from app.evidence_policy import (
    OWNER_SCOPED,
    Capability,
    has_capability,
    verify_identity,
)

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


@dataclass(frozen=True, slots=True)
class _DictRowView:
    """Adapt a projection ``dict`` to the ``evidence_policy.RowView`` protocol.

    Exists so :func:`app.evidence_policy.has_capability` stays the SINGLE capability
    gate — the grain helpers below never re-derive "is this profile granted?" from
    the registry, they ask the same function every other consumer asks.
    """

    data: dict

    def effective_profile_id(self) -> str | None:
        if not verify_identity(self.data):
            return None
        return self.data.get("analysis_profile_id")

    def is_effectively_authoritative(self) -> bool:
        return _effectively_authoritative(self.data)

    def identity_values(self) -> dict:
        return {f: self.data.get(f) for f in IDENTITY_FIELDS}


def owner_scope_ok(
    data: dict, capability: Capability, viewer_user_id: int | None
) -> bool:
    """Submitter scoping for one (row, capability, viewer) triple (g-v21l).

    Effectively AUTHORITATIVE rows always pass: canonical evidence is
    server-produced, carries no associations, and is the same fact for everyone.
    A capability outside :data:`app.evidence_policy.OWNER_SCOPED` (today only
    DISPLAY_OVERLAY) also always passes — it is unscoped by decision.

    Every other case requires this viewer to hold an association for this row,
    read from the row's OWN immutable ``viewer_associated`` snapshot rather than a
    query issued during ranking. ``viewer_user_id=None`` means "no viewer" and can
    never satisfy an owner-scoped capability on a non-authoritative row.
    """
    if _effectively_authoritative(data):
        return True
    if capability not in OWNER_SCOPED:
        return True
    if viewer_user_id is None:
        return False
    return data.get("viewer_associated") is True


def _capability_ok(
    data: dict, capability: Capability, viewer_user_id: int | None
) -> bool:
    """``has_capability`` layered with the owner-scope predicate, in one place."""
    return has_capability(_DictRowView(data), capability) and owner_scope_ok(
        data, capability, viewer_user_id
    )


def position_contract_ok(data: dict) -> bool:
    """POSITION-grain contract satisfaction alone (no capability, no viewer)."""
    cid = data.get("evidence_contract_id")
    return (
        cid == POSITION_COMPLETE and contract_satisfied(POSITION_COMPLETE, data)
    ) or legacy_v2_satisfies_position(data)


def move_contract_ok(data: dict) -> bool:
    """MOVE-grain contract satisfaction alone (no capability, no viewer)."""
    cid = data.get("evidence_contract_id")
    return (
        cid == MOVE_COMPLETE and contract_satisfied(MOVE_COMPLETE, data)
    ) or legacy_v2_satisfies_move(data)


def position_trust_flags(
    data: dict, capability: Capability, viewer_user_id: int | None
) -> tuple[bool, bool, bool]:
    """``(authoritative, contract_satisfied, position_trusted)`` for a position row.

    ``contract_satisfied`` is gated on the position grain SPECIFICALLY: a native
    ``position-complete-v1`` row, OR a legacy ``resolver-complete-v2`` row whose
    position projection is complete. A row that declares another contract (e.g.
    ``move-complete-v1``) is not position-satisfied here even when authoritative.

    ``position_trusted`` (element 3) is NO LONGER "authoritative and satisfied"
    (g-v21l): it is ``contract_satisfied AND has_capability(row, capability) AND
    owner_scope_ok(row, capability, viewer_user_id)``. Canonical rows hold every
    capability for every viewer, so their behavior is byte-for-byte unchanged;
    a granted non-canonical profile can now satisfy its granted capabilities, but
    only for a viewer who independently submitted the same tuple.

    ``capability`` and ``viewer_user_id`` are REQUIRED — there is no permissive
    default, because a caller that forgot to name its consumer would silently
    widen trust.
    """
    satisfied = position_contract_ok(data)
    return (
        _effectively_authoritative(data),
        satisfied,
        satisfied and _capability_ok(data, capability, viewer_user_id),
    )


def move_trust_flags(
    data: dict, capability: Capability, viewer_user_id: int | None
) -> tuple[bool, bool, bool]:
    """``(authoritative, contract_satisfied, move_trusted)`` for a move-grain row.

    ``contract_satisfied`` is gated on the move grain SPECIFICALLY: a native
    ``move-complete-v1`` row, OR a legacy ``resolver-complete-v2`` row whose move
    projection is complete. A native ``position-complete-v1`` row is not
    move-satisfied here even when authoritative.

    ``move_trusted`` follows the same capability + owner-scope rule as
    :func:`position_trust_flags`; see its docstring.
    """
    satisfied = move_contract_ok(data)
    return (
        _effectively_authoritative(data),
        satisfied,
        satisfied and _capability_ok(data, capability, viewer_user_id),
    )


# Identity columns both projectors copy so the trust helpers can verify identity.
_IDENTITY_PROJECTION = ("analysis_profile_id", "evidence_contract_id", *IDENTITY_FIELDS)


def cache_row_as_position_dict(row, *, viewer_associated: bool = False) -> dict:
    """Project an ``analysis_cache`` row into the position-grain trust dict.

    Reads attributes via ``getattr`` (no ORM import). ``best_line_uci`` stays in its
    space-joined storage form — the contract validators accept the string.

    ``viewer_associated`` is THIS request's viewer's membership for this row, not
    the row's association set; it defaults False so a caller that never resolved
    associations fails closed on every owner-scoped capability.
    """
    data = {f: getattr(row, f, None) for f in _IDENTITY_PROJECTION}
    data["best_move_uci"] = getattr(row, "best_move_uci", None)
    data["best_line_uci"] = getattr(row, "best_line_uci", None)
    data["best_eval"] = getattr(row, "best_eval", None)
    data["best_eval_mate"] = getattr(row, "best_eval_mate", None)
    data["viewer_associated"] = viewer_associated
    return data


def cache_row_as_move_dict(row, *, viewer_associated: bool = False) -> dict:
    """Project an ``analysis_cache`` row into the move-grain trust dict."""
    data = {f: getattr(row, f, None) for f in _IDENTITY_PROJECTION}
    data["played_eval"] = getattr(row, "played_eval", None)
    data["played_eval_mate"] = getattr(row, "played_eval_mate", None)
    data["classification"] = getattr(row, "classification", None)
    data["eval_delta"] = getattr(row, "eval_delta", None)
    data["viewer_associated"] = viewer_associated
    return data


# --- g-v21l: the resolved-evidence descriptor ----------------------------------


POSITION_GRAIN = "position"
MOVE_GRAIN = "move"

CACHE_SOURCE = "analysis_cache"
POSITION_STORAGE_SOURCE = "position_analysis"


@dataclass(frozen=True, slots=True)
class ResolvedEvidence:
    """Immutable provenance of the row a read actually resolved (g-v21l).

    Replaces the profile-only provenance that used to ride on ``TrustedPosition``.
    It is BACKEND-ONLY: neither this descriptor, its identity snapshot, its
    association membership, its source primary key, nor any capability internal is
    ever exposed in an API response.

    Every field is captured ONCE, at resolution, from the winning row:

    * ``source_table`` + ``source_id`` identify the row exactly, so a consumer can
      prove whether a position grain and a move grain came from the SAME row rather
      than two rows that merely agree;
    * the claimed profile, whether identity verified, the effective profile id and
      the effective-authority result;
    * the declared contract id and the GRAIN-specific satisfaction result;
    * ``viewer_associated`` — whether THIS request's viewer holds an association.
      Only this viewer's membership, never the row's full association set: a read
      resolves for exactly one viewer, and the association fetch is viewer-scoped.
      Full-set loading is reserved for the locked writer's claim pass and the
      opening digest's shared projection;
    * ``identity`` — every :data:`IDENTITY_FIELDS` value INCLUDING nulls.

    It implements the ``evidence_policy.RowView`` methods from that captured
    snapshot, so ``has_capability``, ``compare_row_strength``, and
    ``compare_evidence_rows`` operate on the exact winning row rather than
    reloading it or reconstructing settings from ``analysis_profile_id``.
    """

    grain: str
    source_table: str
    source_id: int | None
    analysis_profile_id: str | None
    identity_verified: bool
    effective_profile: str | None
    effectively_authoritative: bool
    evidence_contract_id: str | None
    contract_satisfied: bool
    viewer_associated: bool
    identity: tuple[tuple[str, object], ...]

    # --- RowView ---
    def effective_profile_id(self) -> str | None:
        return self.effective_profile

    def is_effectively_authoritative(self) -> bool:
        return self.effectively_authoritative

    def identity_values(self) -> dict:
        return dict(self.identity)

    # --- capability + owner scope, decided from the captured snapshot ---
    def holds(self, capability: Capability, viewer_user_id: int | None) -> bool:
        """Grain contract satisfied AND capability held AND owner scope OK."""
        if not self.contract_satisfied:
            return False
        if not has_capability(self, capability):
            return False
        if self.effectively_authoritative:
            return True
        if capability not in OWNER_SCOPED:
            return True
        if viewer_user_id is None:
            return False
        return self.viewer_associated

    def same_source(self, other: "ResolvedEvidence") -> bool:
        """True when two descriptors name the SAME stored row (table + key)."""
        return (
            self.source_id is not None
            and self.source_table == other.source_table
            and self.source_id == other.source_id
        )


def _describe(
    data: dict,
    *,
    grain: str,
    source_table: str,
    source_id: int | None,
    contract_ok: bool,
    viewer_associated: bool,
) -> ResolvedEvidence:
    identity_verified = verify_identity(data)
    return ResolvedEvidence(
        grain=grain,
        source_table=source_table,
        source_id=source_id,
        analysis_profile_id=data.get("analysis_profile_id"),
        identity_verified=identity_verified,
        effective_profile=(
            data.get("analysis_profile_id") if identity_verified else None
        ),
        effectively_authoritative=_effectively_authoritative(data),
        evidence_contract_id=data.get("evidence_contract_id"),
        contract_satisfied=contract_ok,
        viewer_associated=viewer_associated,
        identity=tuple((f, data.get(f)) for f in IDENTITY_FIELDS),
    )


def describe_position_row(
    row, *, source_table: str, viewer_associated: bool = False
) -> ResolvedEvidence:
    """Build the POSITION-grain descriptor for a storage or cache row."""
    data = cache_row_as_position_dict(row, viewer_associated=viewer_associated)
    return _describe(
        data,
        grain=POSITION_GRAIN,
        source_table=source_table,
        source_id=getattr(row, "id", None),
        contract_ok=position_contract_ok(data),
        viewer_associated=viewer_associated,
    )


def describe_move_row(row, *, viewer_associated: bool = False) -> ResolvedEvidence:
    """Build the MOVE-grain descriptor for an ``analysis_cache`` row."""
    data = cache_row_as_move_dict(row, viewer_associated=viewer_associated)
    return _describe(
        data,
        grain=MOVE_GRAIN,
        source_table=CACHE_SOURCE,
        source_id=getattr(row, "id", None),
        contract_ok=move_contract_ok(data),
        viewer_associated=viewer_associated,
    )
