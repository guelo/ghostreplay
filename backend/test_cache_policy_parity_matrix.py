"""Golden storage-policy parity matrix (g-parity-matrix-evpolicy, D11.1).

Every ``(existing row, incoming row)`` pair across 28 archetypes — plus the
missing-key insert column — pinned as the ``(Decision, Reason)`` pair
:func:`decide_analysis_cache_replacement` returns, and compared cell-by-cell
against the SAME matrix captured from the pre-refactor baseline
``be002bfa09ccc95562ea1cfbf9cdb3a0c048597c``.

The refactors under test are ``g-reuse-d21-search`` (comparator reroute,
``declared_profile_inactive`` gate, ``browser-analysis-v1`` retirement,
``browser-analysis-multipv-v2``), ``g-mk1d`` (``CacheRow.metadata``, Rule 2a
measured strength, comparator steps 4-5), and ``g-bgv1-cutover``
(``browser-game-v1`` retirement). Exactly TWO behavior changes were announced
across them, and they are the SAME change applied to two profiles: a valid
incoming row on a RETIRED profile — ``browser-analysis-v1``, then
``browser-game-v1`` — is now refused storage (``keep`` /
``inactive_profile_keep``) whatever it meets. Every other baseline cell must be
byte-identical, and the differing set must equal the announced predicate exactly
— an EXTRA delta is a finding, not a golden refresh.

The archetype spec lives once, in ``scripts/gen_cache_policy_matrix.py``, which
this module loads by path; see that file's docstring for the capture procedure
and the fixture-immutability policy.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from app.analysis_cache_policy import project_cache_row
from app.analysis_profiles import list_profiles

_BACKEND = Path(__file__).resolve().parent
_REPO = _BACKEND.parent
_GENERATOR = _BACKEND / "scripts" / "gen_cache_policy_matrix.py"
_CURRENT_FIXTURE = _BACKEND / "tests" / "fixtures" / "cache_policy_matrix_current.json"
_PRE_FIXTURE = (
    _BACKEND / "tests" / "fixtures" / "cache_policy_matrix_pre_refactor_be002bf.json"
)


def _load_generator():
    # scripts/ is not a package; load the capture tool by path (same pattern as
    # test_accuracy_freeze). main() is guarded by __main__, so no fixture is written.
    spec = importlib.util.spec_from_file_location("gen_cache_policy_matrix", _GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


GEN = _load_generator()

# The archetype ids, pinned LITERALLY. Without this the keyset tests would still
# pass after an archetype was deleted and the fixtures regenerated — the matrix
# would silently shrink while every other assertion stayed green.
CURRENT_IDS = (
    "browser_analysis_core_v2",
    "browser_analysis_core_v2_mate",
    "browser_analysis_invalid",
    "browser_game_core_v1",
    "browser_game_core_v1_mate",
    "browser_game_sparse",
    "canonical_conflict_v2",
    "canonical_core_v1",
    "canonical_core_v2",
    "canonical_core_v2_mate",
    "canonical_invalid",
    "canonical_linux_core_v2",
    "canonical_move_complete",
    "canonical_no_san_v2",
    "canonical_sparse",
    "game_v2_d17",
    "game_v2_d17_other_net",
    "game_v2_d21",
    "game_v2_d21_hash64",
    "game_v2_d21_no_san",
    "jeffml_sparse",
    "legacy_sparse_contracted",
    "legacy_uncontracted",
    "multipv_core_v2",
    "multipv_core_v2_mate",
    "multipv_conflict_v2",
    "multipv_no_san_v2",
    "unverified_canonical",
)

# The subset whose profile was registered at the baseline commit: the two
# canonical profiles, browser-game-v1, browser-analysis-v1, jeffml, and the
# profile-less legacy rows. browser-analysis-multipv-v2 (g-reuse-d21-search) and
# browser-game-v2 (g-mk1d) did not exist there. Pinned literally rather than
# derived from CURRENT_IDS by a name pattern: what belongs here is "registered at
# be002bf", a fact about that commit, not a fact about how an id is spelled.
PRE_REFACTOR_IDS = (
    "browser_analysis_core_v2",
    "browser_analysis_core_v2_mate",
    "browser_analysis_invalid",
    "browser_game_core_v1",
    "browser_game_core_v1_mate",
    "browser_game_sparse",
    "canonical_conflict_v2",
    "canonical_core_v1",
    "canonical_core_v2",
    "canonical_core_v2_mate",
    "canonical_invalid",
    "canonical_linux_core_v2",
    "canonical_move_complete",
    "canonical_no_san_v2",
    "canonical_sparse",
    "jeffml_sparse",
    "legacy_sparse_contracted",
    "legacy_uncontracted",
    "unverified_canonical",
)

# The announced behavior changes, as a literal count: 5 valid incoming archetypes
# on a RETIRED profile x 20 operands (19 existing + missing key) = 100. That is 2
# browser-analysis-v1 archetypes (40) plus 3 browser-game-v1 archetypes (60).
ANNOUNCED_DELTA_COUNT = 100

# Profiles retired from WRITES. Their rows stay readable and keep their dominance
# edges; only an INCOMING row on one of them is refused storage.
RETIRED_PROFILES = frozenset({
    "browser-analysis-v1",  # g-reuse-d21-search
    "browser-game-v1",      # g-bgv1-cutover
})


def _load(path: Path) -> dict:
    with path.open() as fh:
        return json.load(fh)


def _archetypes_by_id() -> dict:
    return {a.id: a for a in GEN.ARCHETYPES}


def _is_announced_retirement_delta(existing_id: str, incoming_id: str) -> bool:
    """The announced deltas: a VALID incoming row on a RETIRED profile.

    Retirement refuses it storage whatever it meets — including the missing-key
    insert column — so the predicate ignores ``existing`` entirely. An INVALID row
    on a retired profile is deliberately excluded: the validity gate precedes the
    inactive gate, so ``browser_analysis_invalid`` kept ``invalid_incoming_keep``
    in both trees. All three browser-game-v1 archetypes happen to be valid, so the
    conjunct excludes no browser-game cell — it is kept because it states the RULE
    rather than the current archetype census.
    """
    incoming = _archetypes_by_id()[incoming_id]
    if incoming.profile_id not in RETIRED_PROFILES:
        return False
    return incoming.contract_satisfied and incoming.identity_verified


def _cross_product(ids) -> set[str]:
    return {f"None|{i}" for i in ids} | {f"{e}|{i}" for i in ids for e in ids}


def _split(key: str) -> tuple[str, str]:
    existing, _, incoming = key.partition("|")
    return existing, incoming


# --- archetype spec integrity ---------------------------------------------------


def test_archetypes_cover_every_registered_profile():
    # Registering a profile without extending the matrix must fail HERE: an
    # unexercised profile is an unpinned corner of the replacement policy.
    exercised = {a.profile_id for a in GEN.ARCHETYPES}
    registered = {p.profile_id for p in list_profiles()} | {None}
    assert exercised == registered


def test_every_archetype_is_a_reachable_row():
    # Each archetype must be a row a writer could really persist: the projector,
    # not the spec, decides identity_verified / contract_satisfied. Full dataclass
    # equality (values, populated_fields, metadata included) so a declared boolean
    # can never disagree with what project_cache_row actually derives — otherwise
    # the matrix could pin an unreachable state forever.
    for archetype in GEN.ARCHETYPES:
        assert project_cache_row(archetype.data) == archetype.row, archetype.id


def test_archetype_spec_exercises_both_falsy_flags():
    # The matrix is only a validity-gate pin if some archetype actually fails each
    # gate. Guards against a future spec edit quietly making every row valid.
    assert any(not a.contract_satisfied for a in GEN.ARCHETYPES)
    assert any(not a.identity_verified for a in GEN.ARCHETYPES)


# --- current golden --------------------------------------------------------------


def test_current_golden_is_the_full_cross_product():
    golden = _load(_CURRENT_FIXTURE)
    assert set(golden) == _cross_product(CURRENT_IDS)
    assert len(golden) == len(CURRENT_IDS) * (len(CURRENT_IDS) + 1) == 812


def test_current_matrix_matches_golden():
    golden = _load(_CURRENT_FIXTURE)
    actual = GEN.build_matrix()
    differing = {k: (v, actual.get(k)) for k, v in golden.items() if actual.get(k) != v}
    added = set(actual) - set(golden)
    assert not differing and not added, "\n".join(
        [f"{k}: {want} -> {got}" for k, (want, got) in sorted(differing.items())]
        + [f"{k}: (absent) -> {actual[k]}" for k in sorted(added)]
    )


def test_generator_reproduces_current_golden():
    # Capture idempotence: re-running the committed generator must reproduce the
    # committed bytes, through the SAME serializer the comparison uses.
    assert GEN.dumps_matrix(GEN.build_matrix()) == _CURRENT_FIXTURE.read_text()


# --- pre-refactor parity ---------------------------------------------------------


def test_pre_refactor_golden_is_the_full_cross_product():
    golden = _load(_PRE_FIXTURE)
    assert set(golden) == _cross_product(PRE_REFACTOR_IDS)
    assert len(golden) == len(PRE_REFACTOR_IDS) * (len(PRE_REFACTOR_IDS) + 1) == 380


def test_parity_with_pre_refactor_except_announced_deltas():
    pre = _load(_PRE_FIXTURE)
    current = _load(_CURRENT_FIXTURE)
    for key, before in sorted(pre.items()):
        assert key in current, f"{key} vanished from the current matrix"
        after = current[key]
        if _is_announced_retirement_delta(*_split(key)):
            assert after == ["keep", "inactive_profile_keep"], key
            # The exception must be EXERCISED, never a silent pass: if the cell
            # already read this way before the refactor it does not belong in the
            # announced set.
            assert after != before, f"{key} is in the announced set but did not move"
        else:
            assert after == before, f"{key}: {before} -> {after} (unannounced)"


def test_announced_delta_set_is_exactly_the_retirement_predicate():
    pre = _load(_PRE_FIXTURE)
    current = _load(_CURRENT_FIXTURE)
    observed = {k for k, v in pre.items() if current[k] != v}
    predicted = {k for k in pre if _is_announced_retirement_delta(*_split(k))}
    assert observed == predicted
    assert len(observed) == ANNOUNCED_DELTA_COUNT


# --- the announced behaviors, in readable form -----------------------------------


@pytest.mark.parametrize(
    "existing,incoming,expected",
    [
        # The corrective successor replaces the defective hidden protocol for an
        # exact key, INDEPENDENT of numeric strength (PROTOCOL_CORRECTION edge)...
        ("browser_analysis_core_v2", "multipv_core_v2",
         ["replace", "protocol_corrected_replace"]),
        # ...and still replaces the weaker d17 game row (TIER_BASELINE edge).
        ("browser_game_core_v1", "multipv_core_v2",
         ["replace", "dominates_replace"]),
        # Guarded negatives: it never crosses the authority barrier...
        ("canonical_core_v2", "multipv_core_v2", ["keep", "incompatible_keep"]),
        # ...and never reclaims legacy/unidentified evidence (non-authoritative).
        ("legacy_uncontracted", "multipv_core_v2", ["keep", "legacy_keep_non_auth"]),
        ("unverified_canonical", "multipv_core_v2", ["keep", "legacy_keep_non_auth"]),
        # A corrective edge never licenses DROPPING evidence: the completeness
        # gate vetoes the win.
        ("browser_analysis_core_v2", "multipv_no_san_v2",
         ["keep", "incoming_less_complete_keep"]),
    ],
)
def test_announced_multipv_cells(existing, incoming, expected):
    assert _load(_CURRENT_FIXTURE)[f"{existing}|{incoming}"] == expected


@pytest.mark.parametrize(
    "existing,incoming,expected",
    [
        # Rule 2a measured strength across two devices on one dynamic profile.
        ("game_v2_d17", "game_v2_d21", ["replace", "strength_replace"]),
        ("game_v2_d21", "game_v2_d17", ["keep", "strength_weaker_keep"]),
        # Equal depth, different provenance: not mergeable, so first wins.
        ("game_v2_d21", "game_v2_d21_hash64", ["keep", "same_profile_idempotent"]),
        # Different net: unrankable however deep either side searched.
        ("game_v2_d17", "game_v2_d17_other_net",
         ["keep", "strength_incomparable_keep"]),
        # Stronger search that would drop a field loses to the completeness guard.
        ("game_v2_d17", "game_v2_d21_no_san",
         ["keep", "incoming_less_complete_keep"]),
        # An all-None browser-game-v1 row is UNKNOWN strength, never "weaker": v2
        # must not reclaim legacy d17 rows by depth.
        ("browser_game_core_v1", "game_v2_d21", ["keep", "incompatible_keep"]),
    ],
)
def test_announced_dynamic_strength_cells(existing, incoming, expected):
    assert _load(_CURRENT_FIXTURE)[f"{existing}|{incoming}"] == expected


# --- baseline provenance ---------------------------------------------------------


def _baseline_history_available() -> bool:
    """True when this checkout can reach the pinned commit at all.

    The ONLY three conditions that may skip the provenance test: no git binary, no
    git checkout (a source tarball), or a shallow clone missing the pinned commit.
    Each means the baseline is genuinely unreachable here. Everything past this
    point is a real failure — see the test below.
    """
    if shutil.which("git") is None or not (_REPO / ".git").exists():
        return False
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{GEN.BASELINE_COMMIT}^{{commit}}"],
        cwd=_REPO,
        capture_output=True,
    )
    return probe.returncode == 0


def test_baseline_fixture_reproduces_from_pinned_commit():
    """Regenerate the baseline fixture from the pinned commit and byte-compare.

    Provenance is VERIFIED, not documented: this is the only gate that catches an
    archetype whose SHAPE changed under an existing id (the keyset tests cannot —
    the keys are unchanged). The committed HEAD generator runs with ``PYTHONPATH``
    pointed at a throwaway worktree, so nothing is written into it and the shared
    working tree is never touched.

    Once the pinned commit is known to be reachable, every later step FAILS rather
    than skips. A skip here is indistinguishable from a pass in the suite summary,
    which would report green while the drift guard never ran — the exact failure
    mode a read-only ``.git`` produced in review.
    """
    if not _baseline_history_available():
        pytest.skip(
            f"no git checkout reaching {GEN.BASELINE_COMMIT[:12]} "
            "(missing git binary, not a checkout, or a shallow clone)"
        )

    tmp = tempfile.mkdtemp(prefix="cache_policy_baseline_")
    # A unique BASENAME, not merely a unique path: `git worktree add` keys its
    # admin entry by basename under .git/worktrees/, so two concurrent runs in
    # this repo (multi-agent workspace) would contend for one name. git does
    # auto-suffix a duplicate, but relying on that leaves the entry unattributable
    # to the run that made it, and a killed run's orphan indistinguishable.
    worktree = Path(tmp) / f"baseline_{Path(tmp).name.rpartition('_')[2]}_{os.getpid()}"
    added = False
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), GEN.BASELINE_COMMIT],
            cwd=_REPO,
            capture_output=True,
            text=True,
        )
        assert add.returncode == 0, (
            "could not create the baseline worktree, so the archetype-shape drift "
            "guard did not run. This is a hard failure, never a skip: a read-only "
            "or locked .git must not be reported as a passing suite.\n"
            f"{add.stderr.strip()}"
        )
        # Set BEFORE the sanity check below: a zero exit means git registered the
        # admin entry, so it must be removed even if the checkout looks wrong.
        added = True
        assert worktree.is_dir(), f"git reported success but {worktree} is missing"

        out = Path(tmp) / "captured.json"
        env = {**os.environ, "PYTHONPATH": str(worktree / "backend")}
        run = subprocess.run(
            [sys.executable, str(_GENERATOR), "--out", str(out)],
            cwd=_BACKEND,
            env=env,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr
        assert out.read_text() == _PRE_FIXTURE.read_text(), (
            "the baseline fixture is no longer what the pinned commit produces — "
            "an archetype's shape changed under an existing id. Per the fixture "
            "policy this is a finding: give the changed scenario a NEW id rather "
            "than re-capturing over a pinned cell."
        )
    finally:
        if added:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=_REPO,
                capture_output=True,
            )
        shutil.rmtree(tmp, ignore_errors=True)
