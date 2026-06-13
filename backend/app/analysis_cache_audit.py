"""Pure classification of analysis_cache rows into trust/repair categories.

The cache write guard (:mod:`app.analysis_cache_policy`) only protects *new*
writes. Rows that predate the guard — game-overwritten depth-17 results, partial
legacy precompute rows, or rows that claim a profile they cannot back up — can
still sit in the table preserving unstable best moves, deltas, and
classifications. Those values feed the eval-delta / win-chance fallbacks (e.g.
``app/opening_evidence.py``) without any trust validation, even though they never
count as ``trusted_for_resolution`` hits.

This module is the single, tested place that decides what *category* a stored row
falls into and whether the repair tool should invalidate it. It is deliberately
pure: it consumes a plain dict projection of a row (the same shape
``_row_to_dict`` produces) and the existing profile/contract/policy primitives,
so it can be unit-tested without a database and reused by
``scripts/repair_analysis_cache.py``.

Invalidation policy (see ``g-repair-drill-cache``)
--------------------------------------------------
The classifier is anchored on ONE predicate: would the *current* write guard
accept this row if it arrived as a write today? That is exactly
:func:`incoming_is_valid` — contract satisfied AND no unverifiable profile claim.
A row that fails it is an artifact the guard would reject; it is invalidation
material regardless of whether it carries a profile id.

Two opt-in tiers, so the default action is the least surprising:

* :data:`Category.CONTAMINATED_PROFILE_CLAIM` (invalidated **by default**) — the
  row carries a non-null ``analysis_profile_id`` but fails the guard (stored
  identity does not verify, or evidence fails its declared contract). It
  advertises a profile/contract it does not satisfy.
* :data:`Category.LEGACY_INVALID` (invalidated under ``--include-legacy-null``) —
  no profile claim AND the guard still rejects it: a null / unsatisfied evidence
  contract (this includes empty key-only placeholders). These predate the
  contract system; deleting them is more aggressive, so it is opt-in.

Kept in every mode (the guard would accept these as writes today):

* :data:`Category.CANONICAL_TRUSTED` — identity-verified, active authoritative
  profile, resolver-complete-v2. The rows the precompute produces.
* :data:`Category.CANONICAL_RETIRED` — identity-verified authoritative profile
  that is retired, or active-authoritative but on a weaker-than-v2 contract.
* :data:`Category.NON_AUTH_VALID` — a known non-authoritative producer (browser
  upload, JeffML) whose evidence is valid for its declared contract.
* :data:`Category.LEGACY_VALID` — no profile claim but a satisfied evidence
  contract. Reclaimed by a re-run authoritative precompute (dominance REPLACE),
  not by deletion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.analysis_cache_policy import incoming_is_valid, populated_fields_of
from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import RESOLVER_COMPLETE_V2, contract_satisfied

# Evidence fields carried in the projection for overlap/agreement checks.
_EVIDENCE_FIELDS = (
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


class Category(str, Enum):
    CANONICAL_TRUSTED = "canonical_trusted"
    CANONICAL_RETIRED = "canonical_retired"
    NON_AUTH_VALID = "non_auth_valid"
    LEGACY_VALID = "legacy_valid"
    LEGACY_INVALID = "legacy_invalid"
    CONTAMINATED_PROFILE_CLAIM = "contaminated_profile_claim"


# Categories the repair tool invalidates by default (a profile-claiming row the
# write guard would reject as INVALID_INCOMING_KEEP today).
DEFAULT_INVALIDATE = frozenset({Category.CONTAMINATED_PROFILE_CLAIM})

# Additional categories invalidated only with the legacy opt-in (profile-less
# rows the guard would also reject — null/unsatisfied contract).
LEGACY_INVALIDATE = frozenset({Category.LEGACY_INVALID})


def _identity_verified(data: dict) -> bool:
    profile = get_profile(data.get("analysis_profile_id"))
    if profile is None:
        return False
    return all(data.get(f) == getattr(profile, f) for f in IDENTITY_FIELDS)


def _row_projection(data: dict):
    """Build the CacheRow projection the policy's validity gate consumes."""
    from app.analysis_cache_policy import CacheRow

    contract_id = data.get("evidence_contract_id")
    return CacheRow(
        analysis_profile_id=data.get("analysis_profile_id"),
        evidence_contract_id=contract_id,
        identity_verified=_identity_verified(data),
        contract_satisfied=contract_satisfied(contract_id, data),
        populated_fields=populated_fields_of(data),
        values={f: data.get(f) for f in _EVIDENCE_FIELDS},
    )


def classify_row(data: dict) -> Category:
    """Classify a single cache-row dict into a repair :class:`Category`.

    Anchored on :func:`incoming_is_valid`: a row the write guard would reject is
    invalidation material; the only branch is whether it makes a profile claim
    (default-invalidate) or is profile-less legacy (opt-in invalidate).
    """
    profile_id = data.get("analysis_profile_id")
    valid = incoming_is_valid(_row_projection(data))

    if not valid:
        # The guard rejects it. A profile claim it cannot back up is contaminated;
        # an otherwise-rejected profile-less row is legacy invalidation material.
        if profile_id is not None:
            return Category.CONTAMINATED_PROFILE_CLAIM
        return Category.LEGACY_INVALID

    # Guard would accept it. Sub-categorize the kept rows for reporting.
    if profile_id is None:
        return Category.LEGACY_VALID

    profile = get_profile(profile_id)
    if profile is not None and profile.authoritative:
        if (
            profile.active
            and data.get("evidence_contract_id") == RESOLVER_COMPLETE_V2
        ):
            return Category.CANONICAL_TRUSTED
        return Category.CANONICAL_RETIRED
    return Category.NON_AUTH_VALID


def should_invalidate(category: Category, *, include_legacy_null: bool) -> bool:
    """Whether a row of ``category`` should be deleted under the given options."""
    if category in DEFAULT_INVALIDATE:
        return True
    if include_legacy_null and category in LEGACY_INVALIDATE:
        return True
    return False


@dataclass
class AuditReport:
    """Memory-bounded per-category tally (no per-row keys retained)."""

    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0

    def record(self, category: Category) -> None:
        self.counts[category.value] = self.counts.get(category.value, 0) + 1
        self.total += 1

    def invalidate_count(self, *, include_legacy_null: bool) -> int:
        return sum(
            n
            for cat, n in self.counts.items()
            if should_invalidate(Category(cat), include_legacy_null=include_legacy_null)
        )

    def as_dict(self, *, include_legacy_null: bool) -> dict:
        return {
            "total": self.total,
            "counts": dict(sorted(self.counts.items())),
            "invalidate_count": self.invalidate_count(
                include_legacy_null=include_legacy_null
            ),
        }
