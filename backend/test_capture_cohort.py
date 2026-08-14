"""Tests for the capture-cohort producer (g-p4ih-capture).

Covers, IN PROCESS (no launcher, no live scoring): validate_capture_candidate's
load-guard + score-shape orchestration and its NARROW return type; the precondition
refusals and their precedence; the producer source/runtime fence pieces; private-temp
modes; the two mutual-exclusion locks; orphan detection; the filesystem-error boundary;
argument validation; and runtime-binding parity.

The other three modules cover what this one structurally cannot:
  * test_capture_cohort_pg.py — the fence and publication mechanics against a real
    PostgreSQL snapshot, plus the self-check with real scoring.
  * test_capture_launcher_isolation.py — the hostile-startup vectors, in real subprocesses
    through capture_cohort.sh (they are invisible from inside pytest, whose interpreter has
    already run site.py).
  * test_capture_end_to_end.py — a full successful capture from a throwaway clone with a
    clean committed tree.

Scoring is stubbed where a fully-scorable cohort is not the thing under test: the scorer
has its own tests, and this module tests the PRODUCER's orchestration around it.
"""
from __future__ import annotations

import dataclasses
import errno
import hashlib
import inspect
import io
import json
import os
import stat
import types
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.calibrate_opening_scores_v2 as cal
import test_calibrate_opening_scores as tc
from app.opening_transposition_artifact import EMPTY_DENSIFIED_EDGES


@pytest.fixture(autouse=True)
def _explicit_empty_calibration_routing_snapshot(monkeypatch):
    monkeypatch.setattr(
        cal,
        "load_strict_densified_edges",
        lambda _graph: EMPTY_DENSIFIED_EDGES,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _artifact_and_record(graph=None, roots=None, inputs=None):
    """A schema-valid artifact + provenance record bound to graph/roots (real fingerprints),
    returned as (graph, roots, artifact_bytes, provenance_bytes)."""
    graph = graph if graph is not None else tc._bsi_graph()
    roots = roots if roots is not None else tc._bsi_roots()
    inputs = inputs if inputs is not None else tc._bsi_inputs()
    header = cal.ArtifactHeaderInput(
        as_of=tc._FZ_AS_OF,
        graph_fingerprint=graph.fingerprint,
        roots_fingerprint=roots.fingerprint,
        cache_epoch=7,
        captured_model_version="sm-v2-3",
        evidence_derivation_fingerprint=cal.evidence_derivation_fingerprint(),
        capture_scorer_source_digest=tc._FZ_CAPTURE_DIGEST,
        capture_source_revision=tc._FZ_CAPTURE_REVISION,
        capture_python_version=tc._FZ_CAPTURE_PYTHON,
        capture_chess_version=tc._FZ_CAPTURE_CHESS,
    )
    art = cal.freeze_frozen_artifact(inputs, header)
    hdr = json.loads(art)["header"]
    rec = {k: hdr[k] for k in tc._FZ_MIRRORED}
    rec["sha256"] = hashlib.sha256(art).hexdigest()
    prov = cal._canonical_dumps(rec)
    return graph, roots, art, prov


def _stub_scorer(*, quantile_pool=(40.0,), quantile_pools_by_pair=None):
    """A score_overlay replacement returning controlled PairScores: release-guard pairs get
    the opening key (and, for black, the child key); quantile pairs pool ``quantile_pool``
    named scores (or a per-pair override keyed by pair_id)."""

    def stub(user_id, player_color, graph, overlay, roots, config=None, *,
             as_of, pair_id=None, subject_id=None, cohort_role=None,
             routing_snapshot=EMPTY_DENSIFIED_EDGES):
        if cohort_role == "release_guard":
            nsm = {cal.RELEASE_GUARD_OPENING_KEY: 50.0}
            if player_color == "black":
                nsm[cal.RELEASE_GUARD_CHILD_OPENING_KEY] = 60.0
            named_scores = list(nsm.values())
        else:
            pool = (quantile_pools_by_pair or {}).get(pair_id, quantile_pool)
            named_scores = list(pool)
            nsm = {cal.RELEASE_GUARD_OPENING_KEY: pool[0]} if pool else {}
        return cal.PairScore(
            user_id=user_id,
            player_color=player_color,
            named_scores=named_scores,
            named_score_map=dict(nsm),
            named_confidence_map={k: 1.0 for k in nsm},
            observation_total=25,
            pair_id=pair_id,
            subject_id=subject_id,
            cohort_role=cohort_role,
        )

    return stub


def _fake_git(*, clean=True, head="a" * 40, common="/repo/.git", git_dir=None):
    git_dir = git_dir if git_dir is not None else common

    def fake(*args):
        cp = types.SimpleNamespace(returncode=0, stdout="")
        if args and args[0] == "status":
            cp.stdout = "" if clean else " M backend/app/models.py\n M backend/app/fen.py\n"
        elif args and args[0] == "rev-parse":
            if "--git-common-dir" in args:
                cp.stdout = f"{common}\n{git_dir}\n"
            else:
                cp.stdout = f"{head}\n"
        return cp

    return fake


# ---------------------------------------------------------------------------
# validate_capture_candidate
# ---------------------------------------------------------------------------


class TestValidateCaptureCandidate:
    def test_returns_narrow_result_not_selection_inputs(self, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer(quantile_pool=(40.0,)))
        graph, roots, art, prov = _artifact_and_record()
        result = cal.validate_capture_candidate(art, prov, graph=graph, roots=roots)

        assert isinstance(result, cal.CaptureValidationResult)
        assert not isinstance(result, cal.SelectionInputs)
        # nothing selectable escapes
        for banned in ("cohort", "diagnostics", "pairs"):
            assert not hasattr(result, banned)
        assert result.artifact_sha256 == hashlib.sha256(art).hexdigest()
        assert result.pair_count == 4
        assert result.quantile_count == 2
        assert result.as_of == tc._FZ_AS_OF  # clock comes ONLY from the header

    def test_runs_both_release_path_shape_asserts(self, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        calls = {"quantile": 0, "guard": 0}
        real_q, real_g = cal.assert_min_quantile_scores_per_cell, cal.assert_release_guard_score_shape

        def spy_q(pairs, cells):
            calls["quantile"] += 1
            return real_q(pairs, cells)

        def spy_g(pairs, cells, rb):
            calls["guard"] += 1
            return real_g(pairs, cells, rb)

        monkeypatch.setattr(cal, "assert_min_quantile_scores_per_cell", spy_q)
        monkeypatch.setattr(cal, "assert_release_guard_score_shape", spy_g)
        graph, roots, art, prov = _artifact_and_record()
        cal.validate_capture_candidate(art, prov, graph=graph, roots=roots)
        assert calls == {"quantile": 1, "guard": 1}

    def test_uses_current_runtime_binding_helper(self, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        seen = {}
        real = cal._current_runtime_binding

        def spy(graph, roots):
            seen["called"] = (graph, roots)
            return real(graph, roots)

        monkeypatch.setattr(cal, "_current_runtime_binding", spy)
        graph, roots, art, prov = _artifact_and_record()
        cal.validate_capture_candidate(art, prov, graph=graph, roots=roots)
        assert seen["called"] == (graph, roots)

    def test_not_stricter_than_release_zero_pooled_pair_ok(self, monkeypatch):
        # One quantile pair pools ZERO named scores for every cell; the other pools two.
        # The POOLED distribution reaches len >= 2, so it PASSES — exactly as the release
        # path accepts (assert_min_quantile_scores_per_cell reads the pooled distribution).
        graph, roots, art, prov = _artifact_and_record()
        loaded = cal.load_frozen_artifact(art, prov, cal._current_runtime_binding(graph, roots))
        q_ids = [lp.pair_id for lp in loaded.pairs if lp.cohort_role == "quantile"]
        monkeypatch.setattr(
            cal, "score_overlay",
            _stub_scorer(quantile_pools_by_pair={q_ids[0]: (40.0, 41.0), q_ids[1]: ()}),
        )
        result = cal.validate_capture_candidate(art, prov, graph=graph, roots=roots)
        assert result.quantile_count == 2

    def test_fails_when_pooled_below_two_for_a_cell(self, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer(quantile_pool=()))  # zero pooled
        graph, roots, art, prov = _artifact_and_record()
        with pytest.raises(cal.ReleaseGuardShapeError) as exc:
            cal.validate_capture_candidate(art, prov, graph=graph, roots=roots)
        assert "pooled" in str(exc.value)

    def test_load_guard_rejects_sha_mismatch(self):
        graph, roots, art, prov = _artifact_and_record()
        bad_prov = cal._canonical_dumps({**json.loads(prov), "sha256": "0" * 64})
        with pytest.raises(cal.ArtifactIntegrityError):
            cal.validate_capture_candidate(art, bad_prov, graph=graph, roots=roots)

    def test_load_guard_rejects_malformed_bytes(self):
        graph, roots, art, prov = _artifact_and_record()
        with pytest.raises(cal.FrozenArtifactError):
            cal.validate_capture_candidate(b"{not json", prov, graph=graph, roots=roots)

    def test_accepts_trailing_newline_on_record(self, monkeypatch):
        # Capture passes record bytes WITH a trailing newline; validate must accept it.
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        graph, roots, art, prov = _artifact_and_record()
        cal.validate_capture_candidate(art, prov + b"\n", graph=graph, roots=roots)


# ---------------------------------------------------------------------------
# Precondition refusals + precedence
# ---------------------------------------------------------------------------


class TestSharedRejectionCorpusIsWiredToTheSelfCheck:
    """The ARTIFACT bead's whole malformation corpus is the capture self-check's corpus.

    Rather than transcribe ~50 pair/node/edge/telemetry/token/ordering rows into this
    module, the parity assertion lives inside the shared rejection helper that every one of
    them already funnels through (`test_calibrate_opening_scores._fz_reject`). So each of
    those rows asserts BOTH entry points, and a row added to the artifact corpus tomorrow is
    a parity row the day it lands, with nothing to remember.

    That arrangement is only safe if the wiring itself is guarded — otherwise deleting one
    line from a helper in another module silently drops ~50 parity assertions. These tests
    are that guard."""

    def test_the_byte_level_helper_runs_the_producer_entry_point(self):
        # The parity call lives in _fz_reject_bytes, the single byte-level assertion that
        # BOTH _fz_reject and every hand-crafted raw-byte row funnel through.
        source = inspect.getsource(tc._fz_reject_bytes)
        assert "_fz_assert_self_check_parity" in source, (
            "the byte-level rejection helper no longer replays its corpus through "
            "validate_capture_candidate — every artifact malformation row just stopped "
            "being a parity row"
        )

    def test_fz_reject_delegates_to_the_byte_level_helper(self):
        # ...and the payload-perturbing helper must route THROUGH the byte-level one, or its
        # ~50 rows would bypass the parity call above.
        source = inspect.getsource(tc._fz_reject)
        assert "_fz_reject_bytes(" in source, (
            "_fz_reject stopped delegating to _fz_reject_bytes — its rows no longer parity"
        )

    def test_no_rejection_row_bypasses_the_byte_level_helper(self):
        """The teeth: NO test may call load_frozen_artifact inside a `pytest.raises` block —
        that is the exact bypass the round-4 review caught (raw-byte rows asserting the
        loader directly and skipping the self-check). Every rejection must go through
        _fz_reject / _fz_reject_bytes, which parity by construction.

        This is a STATIC scan of the corpus's own AST, so a new bypass added tomorrow fails
        here immediately rather than the next time someone audits by hand."""
        import ast

        def is_pytest_raises(expr):
            return (isinstance(expr, ast.Call)
                    and isinstance(expr.func, ast.Attribute)
                    and expr.func.attr == "raises"
                    and isinstance(expr.func.value, ast.Name)
                    and expr.func.value.id == "pytest")

        def is_load_call(node):
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "load_frozen_artifact")

        tree = ast.parse(inspect.getsource(tc))
        # The ONE legitimate load-inside-raises is _fz_reject_bytes's own assertion — the
        # shared site every other row must reach THROUGH. Exclude only its line range.
        helper = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "_fz_reject_bytes")
        helper_lines = range(helper.lineno, helper.end_lineno + 1)

        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.With) and any(
                is_pytest_raises(item.context_expr) for item in node.items
            ):
                offenders += [c.lineno for c in ast.walk(node)
                              if is_load_call(c) and c.lineno not in helper_lines]

        assert offenders == [], (
            "load_frozen_artifact is asserted inside `pytest.raises` directly instead of via "
            f"_fz_reject/_fz_reject_bytes at source-relative line(s) {sorted(offenders)}, so "
            "those rows skip the producer self-check parity — route them through the helper"
        )

    def test_the_corpus_is_not_empty(self):
        """A guard that passes because nothing calls the helpers would be worthless."""
        corpus_source = inspect.getsource(tc)
        assert corpus_source.count("_fz_reject(") > 40
        assert corpus_source.count("_fz_reject_bytes(") > 10

    def test_the_parity_helper_fails_when_the_self_check_accepts(self, monkeypatch):
        """The guard's own guard: prove _fz_assert_self_check_parity actually fails on the
        failure mode it exists for (a self-check that accepts what the loader rejects)."""
        monkeypatch.setattr(
            cal, "validate_capture_candidate",
            lambda ab, pb, *, graph, roots: "accepted anyway",
        )
        with pytest.raises(AssertionError, match="ACCEPTED bytes the load guard rejected"):
            tc._fz_assert_self_check_parity(
                b"{}", b"{}", (cal.ArtifactSemanticError, "some rejection"))

    def test_the_parity_helper_fails_on_a_different_diagnostic(self, monkeypatch):
        """...and on a self-check that rejects, but not for the same reason. A producer
        that rejects everything would satisfy a weaker assertion."""
        def other(ab, pb, *, graph, roots):
            raise cal.ArtifactSemanticError("a completely different complaint")

        monkeypatch.setattr(cal, "validate_capture_candidate", other)
        with pytest.raises(AssertionError, match="disagree on the same bytes"):
            tc._fz_assert_self_check_parity(
                b"{}", b"{}", (cal.ArtifactSemanticError, "some rejection"))

    def test_the_registry_stubs_reproduce_the_corpus_binding_exactly(self):
        """THE load-bearing invariant for unconditional parity. The corpus loads under
        _fz_rb(); the self-check builds its own binding from the graph/roots it is handed. If
        these two bindings are equal, the self-check applies the IDENTICAL load guard to
        identical bytes, so every rejection row parities with no opt-out — including the
        Phase-C drift rows, which compare header against exactly this binding. If they ever
        diverge, a drift row could reject in the loader for a reason the self-check's binding
        doesn't share, and parity would be silently meaningless. Hence: assert equality."""
        assert cal._current_runtime_binding(tc._FZ_GRAPH_STUB, tc._FZ_ROOTS_STUB) == tc._fz_rb()


class TestSelfCheckMalformationParity:
    """The header/provenance half of the same claim, DERIVED rather than transcribed: it
    enumerates the module's own closed key sets (`_HEADER_KEYS`, `_PROVENANCE_KEYS`) plus a
    value-malformation table, so a new required key is covered the moment it is added.

    This complements the shared corpus above with a DERIVED enumeration of every closed key
    set, so a newly added required key is a parity row the moment it lands even if no one
    writes a hand row for it.

    All rows reject inside the load guard, so no scoring runs and the matrix is fast.
    """

    _MALFORMED_HEADER = {
        "schema_version": 99,
        "as_of": "not-a-timestamp",
        "captured_model_version": 17,
        "graph_fingerprint": "not-a-fingerprint",
        "roots_fingerprint": "",
        "evidence_derivation_fingerprint": None,
        "min_observations": -1,
        "pair_count": "two",
        "cohort_rules": "cohort-rules-that-do-not-exist",
        "release_guard_opening_key": "not-a-fen",
        "release_guard_child_opening_key": 5,
        "cache_epoch": "seven",
        "capture_scorer_source_digest": "ABC123",
        "capture_source_revision": "ab" * 19 + "a",
        "capture_python_version": "PyPy 3.12.1",
        "capture_chess_version": "1.11",
    }

    @staticmethod
    def _both(art: bytes, prov: bytes, graph, roots):
        """Run the SAME bytes through the consumer entry point and the producer one,
        returning (loader_outcome, self_check_outcome) as (type, message) pairs."""
        def outcome(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - the comparison IS the assertion
                return type(exc), str(exc)
            return None, None

        binding = cal._current_runtime_binding(graph, roots)
        return (
            outcome(lambda: cal.load_frozen_artifact(art, prov, binding)),
            outcome(lambda: cal.validate_capture_candidate(
                art, prov, graph=graph, roots=roots)),
        )

    def _assert_parity(self, art, prov, graph, roots):
        loader, self_check = self._both(art, prov, graph, roots)
        assert loader[0] is not None, "the row did not reject at all — it proves nothing"
        assert self_check == loader, (
            f"self-check disagreed with the loader:\n  loader={loader}\n  self ={self_check}"
        )
        return loader

    @pytest.mark.parametrize("key", sorted(cal._HEADER_KEYS))
    def test_missing_header_key_rejects_identically(self, key, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        graph, roots, art, prov = _artifact_and_record()
        payload = json.loads(art)
        del payload["header"][key]
        art2 = cal._canonical_dumps(payload)
        record = json.loads(prov)
        record["sha256"] = hashlib.sha256(art2).hexdigest()
        self._assert_parity(art2, cal._canonical_dumps(record), graph, roots)

    @pytest.mark.parametrize("key", sorted(_MALFORMED_HEADER))
    def test_malformed_header_value_rejects_identically(self, key, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        graph, roots, art, prov = _artifact_and_record()
        payload = json.loads(art)
        payload["header"][key] = self._MALFORMED_HEADER[key]
        art2 = cal._canonical_dumps(payload)
        record = json.loads(prov)
        record["sha256"] = hashlib.sha256(art2).hexdigest()
        self._assert_parity(art2, cal._canonical_dumps(record), graph, roots)

    @pytest.mark.parametrize("key", sorted(cal._PROVENANCE_KEYS))
    def test_missing_record_key_rejects_identically(self, key, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        graph, roots, art, prov = _artifact_and_record()
        record = json.loads(prov)
        del record[key]
        self._assert_parity(art, cal._canonical_dumps(record), graph, roots)

    @pytest.mark.parametrize("key", sorted(cal._PROVENANCE_KEYS - {"sha256"}))
    def test_record_disagreeing_with_header_rejects_identically(self, key, monkeypatch):
        """The mirrored fields: a record that disagrees with the header it describes."""
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        graph, roots, art, prov = _artifact_and_record()
        record = json.loads(prov)
        record[key] = self._MALFORMED_HEADER.get(key, "disagreeing-value")
        self._assert_parity(art, cal._canonical_dumps(record), graph, roots)

    def test_sha256_mismatch_rejects_identically(self, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        graph, roots, art, prov = _artifact_and_record()
        record = json.loads(prov)
        record["sha256"] = "0" * 64
        loader, _ = self._both(art, cal._canonical_dumps(record), graph, roots)
        assert loader[0] is cal.ArtifactIntegrityError
        self._assert_parity(art, cal._canonical_dumps(record), graph, roots)

    def test_non_canonical_artifact_bytes_reject_identically(self, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        graph, roots, art, prov = _artifact_and_record()
        # Semantically identical, byte-wise non-canonical (insignificant whitespace).
        art2 = json.dumps(json.loads(art), sort_keys=True, indent=1).encode()
        record = json.loads(prov)
        record["sha256"] = hashlib.sha256(art2).hexdigest()
        loader = self._assert_parity(art2, cal._canonical_dumps(record), graph, roots)
        assert loader[0] is cal.ArtifactCanonicalBytesError

    def test_malformed_bytes_reject_identically(self, monkeypatch):
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer())
        graph, roots, art, prov = _artifact_and_record()
        for bad_art, bad_prov in ((b"{not json", prov), (art, b"{not json"), (b"", prov)):
            self._assert_parity(bad_art, bad_prov, graph, roots)

    def test_the_valid_row_passes_both(self, monkeypatch):
        """The control: without it every assertion above could be satisfied by a
        self-check that rejects everything."""
        monkeypatch.setattr(cal, "score_overlay", _stub_scorer(quantile_pool=(40.0, 41.0)))
        graph, roots, art, prov = _artifact_and_record()
        binding = cal._current_runtime_binding(graph, roots)
        cal.load_frozen_artifact(art, prov, binding)
        assert cal.validate_capture_candidate(art, prov, graph=graph, roots=roots)


class TestPreconditionRefusals:
    def test_isolation_refuses_bare_pytest(self):
        # pytest is not launched through capture_cohort.sh, so isolation refuses FIRST.
        with pytest.raises(cal.CaptureIsolationError) as exc:
            cal._require_capture_isolation()
        assert cal.SCORER_SOURCE_DIGEST_ENV in str(exc.value) or "-S" in str(exc.value)

    def test_main_worktree_refuses_linked(self, monkeypatch):
        monkeypatch.setattr(
            cal, "_git_capture",
            _fake_git(common="/g/.git", git_dir="/g/.git/worktrees/wt"),
        )
        with pytest.raises(cal.CaptureWorktreeError) as exc:
            cal._require_main_worktree()
        assert "LINKED" in str(exc.value) or "linked" in str(exc.value)

    def test_main_worktree_accepts_equal(self, monkeypatch):
        monkeypatch.setattr(cal, "_git_capture", _fake_git(common="/g/.git", git_dir="/g/.git"))
        assert cal._require_main_worktree() == "/g/.git"

    def test_repo_interior_output_refused(self):
        with pytest.raises(cal.CaptureGovernanceError):
            cal._refuse_repo_interior_output(cal._REPO_ROOT / "captured.json")

    def test_output_outside_repo_ok(self):
        # A path OUTSIDE the repo working tree (a sibling of the repo dir) is accepted.
        # (tmp_path can land under backend/.tmp when TMPDIR points into the repo, so use an
        # explicitly-outside path here.)
        outside = cal._REPO_ROOT.parent / "ghostreplay-outside-cohort.json"
        assert cal._refuse_repo_interior_output(outside) == outside.resolve()

    def test_relative_output_refused(self):
        # The launcher execs the child with cwd=<tree>/backend, so a relative --output would
        # resolve against a directory the operator never chose.
        with pytest.raises(cal.CaptureGovernanceError) as exc:
            cal._refuse_repo_interior_output(Path("private/cohort.json"))
        assert "ABSOLUTE" in str(exc.value)

    def test_relative_output_is_not_resolved_against_cwd(self, tmp_path, monkeypatch):
        # Refused, not silently resolved: chdir'ing somewhere valid must not make it pass,
        # because the CHILD's cwd is a different directory than this one.
        monkeypatch.chdir(tmp_path)
        with pytest.raises(cal.CaptureGovernanceError):
            cal._refuse_repo_interior_output(Path("cohort.json"))

    def test_repo_interior_message_is_basename_only(self):
        with pytest.raises(cal.CaptureGovernanceError) as exc:
            cal._refuse_repo_interior_output(cal._REPO_ROOT / "secret_dir" / "captured.json")
        assert "secret_dir" not in str(exc.value)
        assert "captured.json" in str(exc.value)

    def test_dialect_refuses_sqlite(self):
        sf = sessionmaker(bind=create_engine("sqlite://"))
        with pytest.raises(cal.CaptureDialectError):
            cal._require_postgres_dialect(cal._engine_of(sf))


class TestGovernancePrecedence:
    def test_isolation_is_first(self):
        sf = sessionmaker(bind=create_engine("sqlite://"))
        with pytest.raises(cal.CaptureIsolationError):
            cal.capture_cohort(session_factory=sf, output=Path("/tmp/x.json"), release_guard_user=14)

    def test_worktree_before_output_and_dialect(self, monkeypatch):
        monkeypatch.setattr(cal, "_require_capture_isolation", lambda: None)
        monkeypatch.setattr(
            cal, "_git_capture", _fake_git(common="/g/.git", git_dir="/g/.git/worktrees/wt")
        )
        sf = sessionmaker(bind=create_engine("sqlite://"))  # a non-pg dialect that would refuse LATER
        with pytest.raises(cal.CaptureWorktreeError):
            cal.capture_cohort(
                session_factory=sf, output=cal._REPO_ROOT / "x.json", release_guard_user=14
            )

    def test_output_before_dialect(self, monkeypatch):
        monkeypatch.setattr(cal, "_require_capture_isolation", lambda: None)
        monkeypatch.setattr(cal, "_require_main_worktree", lambda: "/g/.git")
        sf = sessionmaker(bind=create_engine("sqlite://"))
        with pytest.raises(cal.CaptureGovernanceError):
            cal.capture_cohort(
                session_factory=sf, output=cal._REPO_ROOT / "x.json", release_guard_user=14
            )


# ---------------------------------------------------------------------------
# Producer source/runtime fence
# ---------------------------------------------------------------------------


class TestSourceFence:
    def test_refuses_non_cpython(self, monkeypatch):
        monkeypatch.setattr(cal.platform, "python_implementation", lambda: "PyPy")
        with pytest.raises(cal.CaptureSourceError) as exc:
            cal._capture_source_fence()
        assert "PyPy" in str(exc.value)

    def test_requires_inherited_launcher_digest(self, monkeypatch):
        monkeypatch.setattr(cal, "_LAUNCHER_SCORER_DIGEST", None)
        with pytest.raises(cal.CaptureSourceError):
            cal._capture_source_fence()

    def test_rejects_mismatched_preexec_digest(self, monkeypatch):
        monkeypatch.setattr(cal, "_LAUNCHER_SCORER_DIGEST", "0" * 64)
        with pytest.raises(cal.CaptureSourceError) as exc:
            cal._capture_source_fence()
        assert "compiling" in str(exc.value)  # the compile-window diagnostic

    def test_clean_tree_refuses_dirty(self, monkeypatch):
        monkeypatch.setattr(cal, "_git_capture", _fake_git(clean=False))
        with pytest.raises(cal.CaptureSourceError) as exc:
            cal._resolve_clean_head_revision()
        assert "DIRTY" in str(exc.value) or "dirty" in str(exc.value)

    def test_clean_tree_returns_head(self, monkeypatch):
        monkeypatch.setattr(cal, "_git_capture", _fake_git(clean=True, head="ab" * 20))
        assert cal._resolve_clean_head_revision() == "ab" * 20

    def test_full_success_stamps_attestation(self, monkeypatch):
        # A clean tree + a matching pre-exec digest -> the four reviewable attestation values.
        # The bytecode / import-origin checks have their own coverage and fail closed under a
        # normal pytest run (bytecode writing is on), so stub them to isolate the attestation
        # assembly + chess-origin + clean-tree resolution under test here.
        monkeypatch.setattr(cal, "_LAUNCHER_SCORER_DIGEST", cal.scorer_source_digest())
        monkeypatch.setattr(cal, "check_scorer_bytecode", lambda: None)
        monkeypatch.setattr(cal, "check_scorer_import_origins", lambda: None)
        monkeypatch.setattr(cal, "_git_capture", _fake_git(clean=True, head="cd" * 20))
        att = cal._capture_source_fence()
        assert att.scorer_source_digest == cal.scorer_source_digest()
        assert att.source_revision == "cd" * 20
        assert att.python_version.startswith("CPython ")
        assert cal._CAPTURE_PYTHON_VERSION_RE.match(att.python_version)
        assert att.chess_version == cal.chess.__version__

    def test_chess_origin_accepts_installed(self):
        cal._assert_chess_from_installed_distribution()  # the real venv chess resolves in RECORD

    def test_chess_origin_rejects_shadow(self, monkeypatch, tmp_path):
        # A backend/chess.py shadow with a matching __version__ is not in the RECORD.
        shadow = tmp_path / "chess.py"
        shadow.write_text('__version__ = "1.11.2"\n')
        monkeypatch.setattr(cal.chess, "__file__", str(shadow))
        with pytest.raises(cal.CaptureSourceError) as exc:
            cal._assert_chess_from_installed_distribution()
        assert "RECORD" in str(exc.value) or "shadow" in str(exc.value)


# ---------------------------------------------------------------------------
# Private temps, orphan detection, locks
# ---------------------------------------------------------------------------


class TestPrivateTemps:
    def test_mode_0600_and_not_final(self, tmp_path):
        final = tmp_path / "cohort.json"
        temp = cal._write_private_temp(final, b"payload")
        assert temp.exists() and not final.exists()
        assert stat.S_IMODE(temp.stat().st_mode) == 0o600
        assert temp.read_bytes() == b"payload"
        assert temp.name.startswith("cohort.json.tmp-")

    def test_0600_under_permissive_umask(self, tmp_path):
        old = os.umask(0o000)
        try:
            temp = cal._write_private_temp(tmp_path / "c.json", b"x")
            assert stat.S_IMODE(temp.stat().st_mode) == 0o600
        finally:
            os.umask(old)

    def test_two_writes_get_distinct_names(self, tmp_path):
        final = tmp_path / "c.json"
        assert cal._write_private_temp(final, b"a") != cal._write_private_temp(final, b"b")

    def test_exact_name_collision_fails_without_overwriting(self, tmp_path, monkeypatch):
        """O_EXCL, proved against the EXACT generated path rather than against the
        unlikelihood of a collision. Distinct names are a convenience; O_EXCL is the
        guarantee — if the temp already exists (another run's live temp, or a planted file)
        capture must refuse rather than truncate somebody else's bytes."""
        monkeypatch.setattr(cal.secrets, "token_hex", lambda n: "f" * (2 * n))
        final = tmp_path / "cohort.json"
        collided = final.with_name(f"cohort.json.tmp-{os.getpid()}-{'f' * 16}")
        collided.write_bytes(b"someone-elses-live-temp")

        with pytest.raises(cal.CapturePublicationError) as exc:
            cal._write_private_temp(final, b"new-payload")

        # NOT truncated, NOT overwritten, NOT unlinked.
        assert collided.read_bytes() == b"someone-elses-live-temp"
        assert not final.exists()
        assert "File exists" in str(exc.value)
        assert str(tmp_path) not in str(exc.value)

    def test_collision_is_the_only_failure_the_next_name_recovers_from(self, tmp_path, monkeypatch):
        """Sanity: the collision is about the NAME, not the destination — a fresh token
        writes normally beside the occupied path."""
        tokens = iter(["f" * 16, "a" * 16])
        monkeypatch.setattr(cal.secrets, "token_hex", lambda n: next(tokens))
        final = tmp_path / "cohort.json"
        final.with_name(f"cohort.json.tmp-{os.getpid()}-{'f' * 16}").write_bytes(b"occupied")
        with pytest.raises(cal.CapturePublicationError):
            cal._write_private_temp(final, b"x")
        temp = cal._write_private_temp(final, b"x")
        assert temp.read_bytes() == b"x"

    def test_stale_temps_logged_not_deleted(self, tmp_path, capsys):
        final = tmp_path / "cohort.json"
        stale = tmp_path / "cohort.json.tmp-999-deadbeef"
        stale.write_bytes(b"x")
        cal._log_stale_temps(final)
        err = capsys.readouterr().err
        assert "cohort.json.tmp-999-deadbeef" in err
        assert stale.exists()  # NOT deleted, NOT adopted


class TestFilesystemErrorBoundary:
    """Every OSError raised against a PRIVATE destination must arrive as a CaptureError
    carrying the errno text and a basename — never as a bare OSError, whose str() renders
    the full path and whose escape would blow through the child's `except CaptureError`."""

    def test_missing_output_directory_is_typed_not_oserror(self, tmp_path):
        # The lock file is the first thing capture creates in the output directory, so an
        # absent/unwritable private store surfaces there.
        common = tmp_path / "common"
        common.mkdir()
        missing = tmp_path / "no_such_private_store" / "cohort.json"
        with pytest.raises(cal.CapturePublicationError) as exc:
            with cal._capture_locks(str(common), missing):
                pass
        assert isinstance(exc.value, cal.CaptureError)

    def test_missing_output_directory_message_does_not_leak_the_path(self, tmp_path):
        common = tmp_path / "common"
        common.mkdir()
        missing = tmp_path / "very_secret_store" / "cohort.json"
        with pytest.raises(cal.CapturePublicationError) as exc:
            with cal._capture_locks(str(common), missing):
                pass
        message = str(exc.value)
        assert "very_secret_store" not in message
        assert str(tmp_path) not in message
        # errno text, not str(OSError) (which would carry the filename)
        assert "No such file or directory" in message

    def test_private_temp_create_failure_is_typed_and_redacted(self, tmp_path):
        missing = tmp_path / "secret_dir" / "cohort.json"
        with pytest.raises(cal.CapturePublicationError) as exc:
            cal._write_private_temp(missing, b"payload")
        assert "secret_dir" not in str(exc.value)
        assert "cohort.json" in str(exc.value)

    def test_private_temp_write_failure_removes_the_temp(self, tmp_path, monkeypatch):
        final = tmp_path / "cohort.json"

        class _Boom(io.BytesIO):
            def write(self, data):  # noqa: D401 - simulates ENOSPC mid-write
                raise OSError(errno.ENOSPC, "No space left on device", str(final))

        monkeypatch.setattr(cal.os, "fdopen", lambda fd, mode: (os.close(fd), _Boom())[1])
        with pytest.raises(cal.CapturePublicationError) as exc:
            cal._write_private_temp(final, b"payload")
        assert "No space left on device" in str(exc.value)
        assert str(tmp_path) not in str(exc.value)
        # The temp did not survive, and nothing was published.
        assert list(tmp_path.glob("cohort.json.tmp-*")) == []
        assert not final.exists()

    def test_publication_error_is_in_the_typed_hierarchy(self):
        assert issubclass(cal.CapturePublicationError, cal.CaptureError)

    def test_no_oserror_cause_is_chained_through(self, tmp_path):
        # `raise ... from None`: the chained OSError would print the full path in a traceback
        # even though the message itself is clean.
        common = tmp_path / "common"
        common.mkdir()
        with pytest.raises(cal.CapturePublicationError) as exc:
            with cal._capture_locks(str(common), tmp_path / "gone" / "c.json"):
                pass
        assert exc.value.__cause__ is None


class TestArgumentValidation:
    def test_zero_max_attempts_is_typed_not_assertionerror(self):
        sf = sessionmaker(bind=create_engine("sqlite://"))
        with pytest.raises(cal.CaptureGovernanceError) as exc:
            cal.capture_cohort(
                session_factory=sf, output=Path("/tmp/x.json"),
                release_guard_user=14, max_attempts=0,
            )
        assert "max_attempts" in str(exc.value)

    def test_negative_max_attempts_is_typed(self):
        sf = sessionmaker(bind=create_engine("sqlite://"))
        with pytest.raises(cal.CaptureGovernanceError):
            cal.capture_cohort(
                session_factory=sf, output=Path("/tmp/x.json"),
                release_guard_user=14, max_attempts=-1,
            )

    def test_max_attempts_validated_before_any_precondition(self):
        # Ahead of the isolation refusal: it costs nothing and touches nothing, and the
        # AssertionError it prevents is not catchable by the subcommand.
        sf = sessionmaker(bind=create_engine("sqlite://"))
        with pytest.raises(cal.CaptureGovernanceError):
            cal.capture_cohort(
                session_factory=sf, output=Path("/tmp/x.json"),
                release_guard_user=14, max_attempts=0,
            )

    def test_cli_rejects_non_positive_max_attempts(self):
        for bad in ("0", "-3"):
            with pytest.raises(ValueError):
                cal._positive_int(bad)
        assert cal._positive_int("1") == 1

    def test_cli_max_attempts_exits_two(self, monkeypatch):
        monkeypatch.setenv("GHOSTREPLAY_RELEASE_GUARD_USER", "14")
        with pytest.raises(SystemExit) as exc:
            cal.main(["capture-cohort", "--output", "/tmp/x.json", "--max-attempts", "0"])
        assert exc.value.code == 2  # argparse usage error, never the retry loop


class TestSessionModeSampling:
    class _Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, _criterion):
            return self

        def all(self):
            return list(self.rows)

    class _DB:
        def __init__(self, rows):
            self.rows = rows

        def query(self, *_columns):
            return TestSessionModeSampling._Query(self.rows)

    @staticmethod
    def _overlay(count):
        overlay = tc._fz_overlay(2, "black", count)
        session_ids = {str(uuid.uuid4()) for _ in range(count)}
        for node in overlay.nodes.values():
            node.session_ids = set(session_ids)
        return overlay

    def test_counts_distinct_contributing_sessions_and_fingerprint_is_order_stable(self):
        overlay = self._overlay(3)
        session_ids = sorted(cal._contributing_session_ids(overlay))
        rows = [(session_ids[0], "normal"), (session_ids[1], "drill"), (session_ids[2], "normal")]
        sample = cal._collect_session_mode_sample(self._DB(rows), overlay)
        reversed_sample = cal._collect_session_mode_sample(self._DB(reversed(rows)), overlay)
        assert sample.counts == cal.SessionModeCounts(normal=2, drill=1)
        assert sample.fingerprint == reversed_sample.fingerprint

    def test_empty_overlay_has_bound_zero_counts(self):
        sample = cal._collect_session_mode_sample(
            self._DB([]), tc.EvidenceOverlay(2, "black")
        )
        assert sample.counts == cal.SessionModeCounts(0, 0)
        assert len(sample.fingerprint) == 64

    def test_missing_or_unknown_session_mode_fails_closed_without_id_in_message(self):
        overlay = self._overlay(2)
        session_ids = sorted(cal._contributing_session_ids(overlay))
        with pytest.raises(cal.CaptureSessionMixError) as missing:
            cal._collect_session_mode_sample(self._DB([(session_ids[0], "normal")]), overlay)
        assert all(session_id not in str(missing.value) for session_id in session_ids)
        with pytest.raises(cal.CaptureSessionMixError) as unknown:
            cal._collect_session_mode_sample(
                self._DB([(session_ids[0], "normal"), (session_ids[1], "other")]), overlay
            )
        assert all(session_id not in str(unknown.value) for session_id in session_ids)

    def test_post_snapshot_missing_mapping_is_retryable_movement(self, monkeypatch):
        pairs = ((2, "white"), (3, "black"))
        overlays = {pair: self._overlay(1) for pair in pairs}
        stable = cal._SessionModeSample(
            counts=cal.SessionModeCounts(1, 0), fingerprint="a" * 64
        )

        def sample(_db, overlay):
            if overlay is overlays[pairs[1]]:
                raise cal.CaptureSessionMixError("row vanished")
            return stable

        monkeypatch.setattr(cal, "_collect_session_mode_sample", sample)
        samples, unclassifiable = cal._collect_post_snapshot_session_mode_samples(
            object(), overlays, pairs
        )
        assert samples == {pairs[0]: stable}
        assert unclassifiable == {pairs[1]}


class TestOrphanDetection:
    def test_mismatch_is_orphan(self, tmp_path, capsys):
        output = tmp_path / "cohort.json"
        output.write_bytes(b"artifact-bytes")
        prov = tmp_path / "cohort_provenance.json"
        prov.write_bytes(json.dumps({"sha256": "0" * 64}).encode())
        assert cal._detect_and_log_orphan(output, prov) is True
        err = capsys.readouterr().err
        assert "cohort.json" in err
        assert str(tmp_path) not in err  # basename only

    def test_match_is_not_orphan(self, tmp_path):
        output = tmp_path / "cohort.json"
        output.write_bytes(b"artifact-bytes")
        prov = tmp_path / "cohort_provenance.json"
        prov.write_bytes(json.dumps({"sha256": hashlib.sha256(b"artifact-bytes").hexdigest()}).encode())
        assert cal._detect_and_log_orphan(output, prov) is False

    def test_no_artifact_is_not_orphan(self, tmp_path):
        output = tmp_path / "cohort.json"
        prov = tmp_path / "cohort_provenance.json"
        prov.write_bytes(json.dumps({"sha256": "0" * 64}).encode())
        assert cal._detect_and_log_orphan(output, prov) is False


class TestMutualExclusionLocks:
    def test_provenance_lock_busy_refuses(self, tmp_path):
        common = tmp_path / "common"
        common.mkdir()
        output = tmp_path / "store" / "cohort.json"
        output.parent.mkdir()
        with cal._capture_locks(str(common), output):
            with pytest.raises(cal.CaptureLockError) as exc:
                with cal._capture_locks(str(common), output):
                    pass
            assert "lock" in str(exc.value)
        # released -> re-acquire cleanly (an flock dies with its process; no stale reap)
        with cal._capture_locks(str(common), output):
            pass

    def test_output_lock_serializes_across_common_dirs(self, tmp_path):
        # The cross-worktree / cross-clone case: different git common dirs, SAME output.
        c1 = tmp_path / "c1"
        c1.mkdir()
        c2 = tmp_path / "c2"
        c2.mkdir()
        output = tmp_path / "store" / "cohort.json"
        output.parent.mkdir()
        with cal._capture_locks(str(c1), output):
            with pytest.raises(cal.CaptureLockError) as exc:
                with cal._capture_locks(str(c2), output):
                    pass
            assert "output" in str(exc.value)

    def test_lock_files_are_named_by_common_dir_and_output_hash(self, tmp_path):
        common = tmp_path / "common"
        common.mkdir()
        output = tmp_path / "store" / "cohort.json"
        output.parent.mkdir()
        with cal._capture_locks(str(common), output):
            assert (common / "cohort-capture.lock").exists()
            out_hash = hashlib.sha256(str(output.resolve()).encode()).hexdigest()
            assert (output.parent / f".cohort-capture-{out_hash}.lock").exists()


# ---------------------------------------------------------------------------
# Byte encoding, runtime-binding parity, operation surface
# ---------------------------------------------------------------------------


class TestByteEncoding:
    def test_provenance_record_is_canonical_plus_one_newline(self):
        # The record's bytes are the artifact's own canonical serializer + exactly one
        # trailing newline (this file is committed to Git); the artifact carries NONE.
        graph, roots, art, prov = _artifact_and_record()
        record = {**json.loads(prov)}
        assert cal._canonical_dumps(record) + b"\n" == prov + b"\n"
        # the artifact bytes carry NO trailing newline
        assert not art.endswith(b"\n")

    def test_capture_record_shape_matches_provenance_keys(self):
        # The record dict capture emits carries exactly the closed provenance key set.
        graph, roots, art, prov = _artifact_and_record()
        assert set(json.loads(prov)) == set(cal._PROVENANCE_KEYS)


class TestRuntimeBindingParity:
    def test_every_field_matches_release_side_binding(self):
        graph = tc._bsi_graph()
        roots = tc._bsi_roots()
        rb = cal._current_runtime_binding(graph, roots)
        # What capture stamps: graph/roots/derivation from the resolved registries; the
        # release-policy pins + schema version from the module constants the freeze uses.
        stamped = {
            "graph_fingerprint": graph.fingerprint,
            "roots_fingerprint": roots.fingerprint,
            "evidence_derivation_fingerprint": cal.evidence_derivation_fingerprint(),
            "min_observations": cal.DEFAULT_MIN_OBSERVATIONS,
            "cohort_rules": cal.COHORT_RULES_ID,
            "release_guard_opening_key": cal.RELEASE_GUARD_OPENING_KEY,
            "release_guard_child_opening_key": cal.RELEASE_GUARD_CHILD_OPENING_KEY,
            "schema_version": cal.ARTIFACT_SCHEMA_VERSION,
        }
        fields = {f.name for f in dataclasses.fields(cal.RuntimeBinding)}
        # A newly added RuntimeBinding field fails HERE until capture is taught to cover it.
        assert set(stamped) == fields
        for name in fields:
            assert stamped[name] == getattr(rb, name), name


class TestOperationSurface:
    def test_no_min_observations_parameter(self):
        params = set(inspect.signature(cal.capture_cohort).parameters)
        assert params == {
            "session_factory", "output", "release_guard_user",
            "require_quiescent_epoch", "max_attempts",
        }
        assert "min_observations" not in params

    def test_capture_result_is_scalars_plus_path(self):
        fields = {f.name for f in dataclasses.fields(cal.CaptureResult)}
        assert fields == {
            "artifact_path", "artifact_sha256", "pair_count", "quantile_count",
            "release_guard_count", "attempts", "orphan_replaced",
            "snapshot_cache_epoch", "current_view_cache_epoch",
        }

    def test_typed_error_hierarchy(self):
        for name in (
            "CaptureIsolationError", "CaptureWorktreeError", "CaptureGovernanceError",
            "CaptureDialectError", "CaptureLockError", "CaptureSourceError",
            "CaptureFenceExhaustedError", "CaptureEpochUnavailableError",
            "CaptureSessionMixError",
            "CaptureReleaseGuardShapeError", "CaptureSelfCheckError",
            "CapturePublicationError", "CaptureInterRenameError",
        ):
            assert issubclass(getattr(cal, name), cal.CaptureError)
