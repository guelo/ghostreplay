"""@pg_required fence + capture-publication tests for capture_cohort (g-p4ih-capture).

These drive capture_cohort against a synthetic PostgreSQL fixture DB. The contract under
test IS PostgreSQL REPEATABLE READ snapshot isolation, so they SKIP cleanly without a
GHOSTREPLAY_TEST_PG_URL and run for real in CI.

Most of them isolate the FENCE + PUBLICATION mechanics, so the two-process isolation
entrypoint, main-worktree check, and source/runtime binding are patched out — those are
proved in real subprocesses by test_capture_launcher_isolation.py (hostile startup vectors)
and test_capture_end_to_end.py (a full launch from a clean committed clone), and repeating
them here would only re-test the monkeypatches. A concurrent evidence write injected between
two collector passes is what proves the fence: the snapshot never sees it, the post-snapshot
re-read does.

The self-check is stubbed on the fence tests, whose seed is deliberately minimal, and NOT
stubbed in the real-scoring section, which seeds a fully scorable cohort and lets
validate_capture_candidate score the candidate bytes across the whole arm grid for real.

Two later sections need a SECOND REAL PROCESS rather than a monkeypatch: the mutual-
exclusion interleavings (a losing capture must refuse before reading any evidence; the
locks must be held across BOTH renames; a SIGKILLed holder must strand nobody) and the
self-check's unexpected-failure boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

import app.opening_cache as oc
from app.models import Base, GameSession, SessionMove, User
from conftest import pg_required

import scripts.calibrate_opening_scores_v2 as cal

GUARD_USER = 14
START_FULL = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
KINGS_PAWN_FULL = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
OPEN_GAME_FULL = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _reset(engine):
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        # The evidence_epoch triggers UPDATE ... WHERE id = 1 and silently no-op without the
        # singleton the TRUNCATE just removed.
        conn.execute(text("INSERT INTO evidence_epoch (id, value) VALUES (1, 0)"))


def _seed_pair(db, user_id: int, color: str, n_moves: int = 1) -> None:
    if db.get(User, user_id) is None:
        db.add(User(id=user_id, username=f"user{user_id}", is_anonymous=True))
        db.flush()
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),
        status="ended",
        result="checkmate_win",
        engine_elo=1500,
        player_color=color,
        is_rated=True,
        session_mode="normal",
    )
    db.add(session)
    db.flush()
    for i in range(n_moves):
        db.add(SessionMove(
            session_id=session.id, move_number=i + 1, color=color,
            move_san="e4", fen_before=START_FULL, fen_after=KINGS_PAWN_FULL,
            eval_delta=10,
        ))
    db.commit()


def _seed_default_cohort(session_factory) -> None:
    """Two release-guard pairs (User-14 white+black) plus two quantile candidates."""
    with session_factory() as db:
        _seed_pair(db, GUARD_USER, "white")
        _seed_pair(db, GUARD_USER, "black")
        _seed_pair(db, 2, "black")
        _seed_pair(db, 3, "white")


# ---------------------------------------------------------------------------
# Harness: patch the launcher/source-binding preconditions, redirect the
# committed provenance path + output outside the repo, stub the self-check.
# ---------------------------------------------------------------------------


class _Env:
    def __init__(self, session_factory, out_dir):
        self.session_factory = session_factory
        self.out_dir = out_dir
        self.output = out_dir / "cohort-out.json"
        self.provenance = out_dir / "cohort_provenance.json"
        self.common = out_dir / "common"
        self.common.mkdir(exist_ok=True)


@pytest.fixture
def capenv(pg_engine, pg_session_factory, monkeypatch):
    _reset(pg_engine)
    out_dir = Path(tempfile.mkdtemp(prefix="ghostreplay-capture-pgtest-", dir=os.path.realpath("/tmp")))
    env = _Env(pg_session_factory, out_dir)

    att = cal._CaptureAttestation(
        scorer_source_digest="a" * 64, source_revision="b" * 40,
        python_version="CPython 3.12.1", chess_version="1.11.2",
    )
    monkeypatch.setattr(cal, "_require_capture_isolation", lambda: None)
    monkeypatch.setattr(cal, "_require_main_worktree", lambda: str(env.common))
    monkeypatch.setattr(cal, "_capture_source_fence", lambda: att)
    monkeypatch.setattr(cal, "COHORT_PROVENANCE_PATH", env.provenance)
    yield env
    import shutil
    shutil.rmtree(out_dir, ignore_errors=True)


def _stub_validate_ok(monkeypatch):
    monkeypatch.setattr(
        cal, "validate_capture_candidate",
        lambda ab, pb, *, graph, roots: cal.CaptureValidationResult(
            hashlib.sha256(ab).hexdigest(), 0, 0, datetime(2020, 1, 1, tzinfo=timezone.utc)
        ),
    )


def _capture(env, **kw):
    return cal.capture_cohort(
        session_factory=env.session_factory,
        output=env.output,
        release_guard_user=GUARD_USER,
        **kw,
    )


def _inject_once(monkeypatch, session_factory, *, target=(GUARD_USER, "white"), fire_on=1):
    """Wrap capture_freshness_snapshot so the ``fire_on``-th call (during the SNAPSHOT pass)
    commits a concurrent evidence_seq bump for ``target`` on a SEPARATE session — invisible
    to the RR snapshot, visible to the post-snapshot re-read."""
    real = cal.capture_freshness_snapshot
    state = {"n": 0}

    def wrapper(db, uid, color):
        state["n"] += 1
        if state["n"] == fire_on:
            with session_factory() as sep:
                oc.bump_evidence_seq(sep, target[0], target[1])
                sep.commit()
        return real(db, uid, color)

    monkeypatch.setattr(cal, "capture_freshness_snapshot", wrapper)
    return state


# ---------------------------------------------------------------------------
# Fence
# ---------------------------------------------------------------------------


@pg_required
def test_quiescent_run_completes_first_attempt(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    result = _capture(capenv)
    assert result.attempts == 1
    assert result.release_guard_count == 2
    assert capenv.output.exists()
    assert capenv.provenance.exists()
    # header cache_epoch is the epoch observed INSIDE the snapshot
    assert result.snapshot_cache_epoch is not None


@pg_required
def test_movement_every_attempt_exhausts_with_no_side_effects(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    # Fire on EVERY snapshot's first bundle: with 4 fence pairs + guard requery, the first
    # bundle recurs each attempt, so movement is injected on all three attempts.
    real = cal.capture_freshness_snapshot

    def wrapper(db, uid, color):
        # bump before the snapshot's first pair each attempt
        if (uid, color) == (GUARD_USER, "white") and db.bind is not None:
            pass
        return real(db, uid, color)

    # Simplest reliable injection: bump on the first call of every snapshot pass. Track via a
    # per-snapshot marker using the READ-ONLY session's identity.
    state = {"seen": set()}

    def wrap2(db, uid, color):
        if id(db) not in state["seen"]:
            state["seen"].add(id(db))
            with capenv.session_factory() as sep:
                oc.bump_evidence_seq(sep, GUARD_USER, "white")
                sep.commit()
        return real(db, uid, color)

    monkeypatch.setattr(cal, "capture_freshness_snapshot", wrap2)

    with pytest.raises(cal.CaptureFenceExhaustedError):
        _capture(capenv, max_attempts=3)
    # No final artifact, no provenance record
    assert not capenv.output.exists()
    assert not capenv.provenance.exists()


@pg_required
def test_retry_then_succeed_on_first_attempt_movement(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    # Inject on the FIRST snapshot's first bundle only.
    real = cal.capture_freshness_snapshot
    state = {"seen": set(), "fired": False}

    def wrap(db, uid, color):
        if not state["fired"] and id(db) not in state["seen"]:
            state["seen"].add(id(db))
            state["fired"] = True
            with capenv.session_factory() as sep:
                oc.bump_evidence_seq(sep, GUARD_USER, "white")
                sep.commit()
        return real(db, uid, color)

    monkeypatch.setattr(cal, "capture_freshness_snapshot", wrap)
    result = _capture(capenv, max_attempts=3)
    assert result.attempts == 2
    assert capenv.output.exists()


@pg_required
def test_guard_pair_movement_triggers_retry(capenv, monkeypatch):
    # A write to a release-guard pair (no quantile pair moved) still retries.
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    _inject_once(monkeypatch, capenv.session_factory, target=(GUARD_USER, "black"))
    result = _capture(capenv, max_attempts=3)
    assert result.attempts == 2


@pg_required
def test_candidate_set_appearance_triggers_retry(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    real = cal.capture_freshness_snapshot
    state = {"seen": set(), "fired": False}

    def wrap(db, uid, color):
        if not state["fired"] and id(db) not in state["seen"]:
            state["seen"].add(id(db))
            state["fired"] = True
            # a NEW candidate pair appears between the snapshot and the re-read
            with capenv.session_factory() as sep:
                _seed_pair(sep, 7, "white")
        return real(db, uid, color)

    monkeypatch.setattr(cal, "capture_freshness_snapshot", wrap)
    result = _capture(capenv, max_attempts=3)
    assert result.attempts == 2


@pg_required
def test_starvation_regression_global_epoch_only_does_not_retry(capenv, monkeypatch):
    # An analysis_cache write advances ONLY the global cache_epoch (no captured pair moves).
    # Default scoped mode must NOT retry (unrelated engine traffic must not starve capture).
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    real = cal.capture_freshness_snapshot
    state = {"seen": set(), "fired": False}

    def wrap(db, uid, color):
        if not state["fired"] and id(db) not in state["seen"]:
            state["seen"].add(id(db))
            state["fired"] = True
            with capenv.session_factory() as sep:
                sep.execute(text(
                    "INSERT INTO analysis_cache (fen_before, move_uci, move_san, played_eval) "
                    "VALUES (:fb, 'g1f3', 'x', 30)"
                ), {"fb": "8/8/8/8/8/8/8/8 w - - 0 1"})
                sep.commit()
        return real(db, uid, color)

    monkeypatch.setattr(cal, "capture_freshness_snapshot", wrap)
    result = _capture(capenv, max_attempts=3)
    assert result.attempts == 1
    # the header stamps the SNAPSHOT epoch, and the re-read observed a later one
    assert result.current_view_cache_epoch != result.snapshot_cache_epoch


@pg_required
def test_strict_mode_retries_on_global_epoch_movement(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    real = cal.capture_freshness_snapshot
    state = {"seen": set(), "fired": False}

    def wrap(db, uid, color):
        if not state["fired"] and id(db) not in state["seen"]:
            state["seen"].add(id(db))
            state["fired"] = True
            with capenv.session_factory() as sep:
                sep.execute(text(
                    "INSERT INTO analysis_cache (fen_before, move_uci, move_san, played_eval) "
                    "VALUES (:fb, 'g1f3', 'x', 30)"
                ), {"fb": "8/8/8/8/8/8/8/8 w - - 0 2"})
                sep.commit()
        return real(db, uid, color)

    monkeypatch.setattr(cal, "capture_freshness_snapshot", wrap)
    result = _capture(capenv, max_attempts=3, require_quiescent_epoch=True)
    assert result.attempts == 2


@pg_required
def test_unavailable_epoch_strict_fails_default_stamps_null(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    # Remove the singleton so current_cache_epoch returns None.
    with capenv.session_factory() as db:
        db.execute(text("DELETE FROM evidence_epoch WHERE id = 1"))
        db.commit()

    with pytest.raises(cal.CaptureEpochUnavailableError):
        _capture(capenv, require_quiescent_epoch=True)

    # default scoped mode completes and stamps cache_epoch as NULL (None)
    result = _capture(capenv)
    assert result.snapshot_cache_epoch is None


@pg_required
def test_release_guard_shape_at_source_fails(capenv, monkeypatch):
    # Only ONE guard color present -> the guard query yields != two {white, black}.
    with capenv.session_factory() as db:
        _seed_pair(db, GUARD_USER, "white")
        _seed_pair(db, 2, "black")
    _stub_validate_ok(monkeypatch)
    with pytest.raises(cal.CaptureReleaseGuardShapeError):
        _capture(capenv)
    assert not capenv.output.exists()


# ---------------------------------------------------------------------------
# Publication gates
# ---------------------------------------------------------------------------


@pg_required
def test_dialect_refuses_non_postgres(monkeypatch, capenv):
    sqlite_factory = sessionmaker(bind=__import__("sqlalchemy").create_engine("sqlite://"))
    with pytest.raises(cal.CaptureDialectError):
        cal.capture_cohort(
            session_factory=sqlite_factory, output=capenv.output, release_guard_user=GUARD_USER
        )


@pg_required
def test_rerun_is_byte_identical(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)

    # as_of is the single legitimate WALL-CLOCK read (stamped inside the fence), so a rerun
    # over unchanged evidence is byte-identical only when the clock is held fixed — pin it so
    # the "no gratuitous working-tree diff" property is what's under test, not the clock.
    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(cal, "datetime", _FixedNow)
    _capture(capenv)
    art1 = capenv.output.read_bytes()
    prov1 = capenv.provenance.read_bytes()
    _capture(capenv)  # unchanged evidence + fixed clock -> byte-identical, no gratuitous diff
    assert capenv.output.read_bytes() == art1
    assert capenv.provenance.read_bytes() == prov1
    # provenance record ends in exactly one newline; artifact carries none
    assert prov1.endswith(b"\n") and not prov1.endswith(b"\n\n")
    assert not art1.endswith(b"\n")


@pg_required
def test_self_check_failure_leaves_prior_untouched(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    _capture(capenv)  # publish a good pair first
    good_art = capenv.output.read_bytes()
    good_prov = capenv.provenance.read_bytes()

    def failing(ab, pb, *, graph, roots):
        raise cal.ArtifactSemanticError("injected self-check failure")

    monkeypatch.setattr(cal, "validate_capture_candidate", failing)
    with pytest.raises(cal.CaptureSelfCheckError):
        _capture(capenv)
    # the pre-existing artifact + record are byte-identical afterwards
    assert capenv.output.read_bytes() == good_art
    assert capenv.provenance.read_bytes() == good_prov
    # no leftover temp files beside either destination
    assert not list(capenv.out_dir.glob("*.tmp-*"))


@pg_required
def test_inter_rename_failure_is_recoverable_mismatched_pair(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    _capture(capenv)  # a matched pair on disk

    real_replace = os.replace
    state = {"n": 0, "active": True}

    def flaky_replace(src, dst):
        if state["active"]:
            state["n"] += 1
            if state["n"] == 2:  # the SECOND rename (record) fails, ONCE
                state["active"] = False
                raise OSError("simulated read-only repo filesystem")
        return real_replace(src, dst)

    # Patch only os.replace via setattr (NOT monkeypatch.undo, which would also revert the
    # capenv fixture's precondition patches); the flag disarms it after the single failure.
    monkeypatch.setattr(cal.os, "replace", flaky_replace)
    with pytest.raises(cal.CaptureInterRenameError):
        _capture(capenv)

    # The on-disk state is now a mismatched pair: the NEW artifact beside the OLD record.
    new_sha = hashlib.sha256(capenv.output.read_bytes()).hexdigest()
    old_record = json.loads(capenv.provenance.read_bytes())
    assert new_sha != old_record["sha256"]  # the load guard would fail closed on this
    # A rerun replaces both files, restoring a matched pair.
    _capture(capenv)
    matched_sha = hashlib.sha256(capenv.output.read_bytes()).hexdigest()
    assert matched_sha == json.loads(capenv.provenance.read_bytes())["sha256"]


@pg_required
def test_published_artifact_is_0600(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    old = os.umask(0o000)  # permissive umask: a umask-dependent impl would fail
    try:
        _capture(capenv)
    finally:
        os.umask(old)
    assert stat.S_IMODE(capenv.output.stat().st_mode) == 0o600


@pg_required
def test_orphan_replaced_on_rerun(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    # Pre-place an artifact whose SHA-256 mismatches a provenance record.
    capenv.output.write_bytes(b"orphan-artifact")
    capenv.provenance.write_bytes(json.dumps({"sha256": "0" * 64}).encode())
    result = _capture(capenv)
    assert result.orphan_replaced is True
    assert hashlib.sha256(capenv.output.read_bytes()).hexdigest() == result.artifact_sha256


@pg_required
def test_concurrent_capture_refused_before_reading(capenv, monkeypatch):
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    # Hold the provenance lock, then a second capture must refuse before any evidence read.
    with cal._capture_locks(str(capenv.common), capenv.output.resolve()):
        with pytest.raises(cal.CaptureLockError):
            _capture(capenv)


# ---------------------------------------------------------------------------
# Fence scope: writes to pairs that are NOT captured still break the window
# ---------------------------------------------------------------------------


@pg_required
def test_threshold_crossing_write_to_a_non_captured_pair_triggers_retry(capenv, monkeypatch):
    """The fence scope is ALL pre-filter raw candidates, not the captured cohort.

    The seeded non-guard pairs sit far below DEFAULT_MIN_OBSERVATIONS (20), so they are in
    ``raw_pairs`` but NOT in the quantile cohort — none of their evidence reaches the
    artifact. A write to one of them during the window must STILL discard the attempt: it is
    exactly the write that could push a pair across the threshold, and a fence scoped to the
    captured cohort alone would publish an artifact whose MEMBERSHIP was decided from a
    view of the world that had already changed."""
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)

    # Prove the premise before testing the claim: this pair is a raw candidate and is not
    # captured (0 quantile pairs at these observation counts).
    with capenv.session_factory() as db:
        assert (2, "black") in cal.list_opening_score_candidate_pairs(db)

    _inject_once(monkeypatch, capenv.session_factory, target=(2, "black"), fire_on=1)
    result = _capture(capenv, max_attempts=3)

    assert result.quantile_count == 0          # the moved pair was never going to be captured
    assert result.attempts == 2                # ...and the window was discarded anyway
    assert result.release_guard_count == 2


@pg_required
def test_threshold_crossing_movement_alone_can_exhaust_the_fence(capenv, monkeypatch):
    """The same movement on every attempt exhausts with NO artifact — a non-captured pair
    cannot be waved through as 'harmless' by an exhaustion path either."""
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    real = cal.capture_freshness_snapshot
    seen: set[int] = set()

    def wrap(db, uid, color):
        if id(db) not in seen:          # once per snapshot pass
            seen.add(id(db))
            with capenv.session_factory() as sep:
                oc.bump_evidence_seq(sep, 2, "black")
                sep.commit()
        return real(db, uid, color)

    monkeypatch.setattr(cal, "capture_freshness_snapshot", wrap)
    with pytest.raises(cal.CaptureFenceExhaustedError):
        _capture(capenv, max_attempts=2)
    assert not capenv.output.exists()
    assert not capenv.provenance.exists()


@pg_required
def test_candidate_disappearance_triggers_retry(capenv, monkeypatch):
    """A pair VANISHING from the pre-filter list is drift too. The appearance case has its
    own test; deletion is the other direction and takes a different code path in the
    comparison (the pair is missing from ``re_present``, so it is not even sample-compared —
    only the set equality catches it)."""
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    real = cal.capture_freshness_snapshot
    fired = {"done": False}

    def wrap(db, uid, color):
        if not fired["done"]:
            fired["done"] = True
            # Delete user 3's evidence on a SEPARATE session: invisible to the RR snapshot,
            # visible to the post-snapshot re-read.
            with capenv.session_factory() as sep:
                sep.execute(text(
                    "DELETE FROM session_moves WHERE session_id IN "
                    "(SELECT id FROM game_sessions WHERE user_id = 3)"
                ))
                sep.execute(text("DELETE FROM game_sessions WHERE user_id = 3"))
                sep.commit()
        return real(db, uid, color)

    monkeypatch.setattr(cal, "capture_freshness_snapshot", wrap)
    result = _capture(capenv, max_attempts=3)
    assert result.attempts == 2

    # The published artifact reflects the world AFTER the deletion, not a torn mix.
    with capenv.session_factory() as db:
        assert (3, "white") not in cal.list_opening_score_candidate_pairs(db)


# ---------------------------------------------------------------------------
# Filesystem failures against a private destination stay inside the typed boundary
# ---------------------------------------------------------------------------


@pg_required
def test_missing_output_directory_is_a_typed_refusal(capenv, monkeypatch):
    """An absent private store must not escape as a bare OSError whose str() carries the
    full private path — the child only catches CaptureError, so that would print a
    traceback naming the private store and exit outside the typed contract."""
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    missing = capenv.out_dir / "no_such_store" / "cohort.json"
    with pytest.raises(cal.CapturePublicationError) as exc:
        cal.capture_cohort(
            session_factory=capenv.session_factory, output=missing,
            release_guard_user=GUARD_USER,
        )
    assert "no_such_store" not in str(exc.value)
    assert str(capenv.out_dir) not in str(exc.value)


@pg_required
def test_first_rename_failure_leaves_the_prior_pair_untouched(capenv, monkeypatch):
    """The artifact rename is the publication point. If it fails, NOTHING has been
    published: the previous artifact bytes and the committed record are both intact, both
    temps are unlinked, and the caller gets a typed error rather than an OSError."""
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)
    _capture(capenv)  # a matched pair on disk
    prior_artifact = capenv.output.read_bytes()
    prior_record = capenv.provenance.read_bytes()

    real_replace = os.replace
    state = {"n": 0, "active": True}

    def flaky_replace(src, dst):
        if state["active"]:
            state["n"] += 1
            if state["n"] == 1:  # the FIRST rename (artifact) fails, ONCE
                state["active"] = False
                raise OSError(2, "No such file or directory", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(cal.os, "replace", flaky_replace)
    with pytest.raises(cal.CapturePublicationError) as exc:
        _capture(capenv)

    assert "No such file or directory" in str(exc.value)
    assert str(capenv.out_dir) not in str(exc.value)
    assert capenv.output.read_bytes() == prior_artifact
    assert capenv.provenance.read_bytes() == prior_record
    # No temp was left behind at either destination.
    assert list(capenv.out_dir.glob("*.tmp-*")) == []


# ---------------------------------------------------------------------------
# The pre-publication self-check with REAL scoring
# ---------------------------------------------------------------------------

# The fence tests above stub the self-check, because a fully-scorable cohort is not what
# they exercise. This section does not: it seeds evidence rich enough to satisfy BOTH
# release-path shape asserts and lets validate_capture_candidate score the candidate bytes
# for real. That is the only way to prove the producer's self-check is the SAME gate the
# release path applies — a stub can only prove it was called.
#
# Requirements the seed has to meet, and why each one is what it is:
#   * >= 2 quantile pairs clearing DEFAULT_MIN_OBSERVATIONS (20), so every required arm-grid
#     cell pools >= 2 named scores (assert_min_quantile_scores_per_cell).
#   * a white and a black release-guard pair carrying RELEASE_GUARD_OPENING_KEY, and the
#     BLACK one additionally carrying RELEASE_GUARD_CHILD_OPENING_KEY — the Caro is Black's
#     defense, so the black guard needs a Caro-Kann line of its own
#     (assert_release_guard_score_shape).
# Real scoring across the arm grid is not cheap; this is minutes, not seconds.

RUY_LOPEZ = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7"]
CARO_KANN = ["e4", "c6", "d4", "d5", "Nc3", "dxe4", "Nxe4", "Bf5", "Ng3", "Bg6"]


def _seed_scorable_pair(db, user_id: int, color: str, *, line=RUY_LOPEZ, sessions: int = 6):
    """Seed real openings-graph positions (FENs generated by python-chess, so they are on
    the graph rather than the single synthetic position the fence tests use)."""
    import chess

    if db.get(User, user_id) is None:
        db.add(User(id=user_id, username=f"user{user_id}", is_anonymous=True))
        db.flush()
    for _ in range(sessions):
        session = GameSession(
            id=uuid.uuid4(), user_id=user_id,
            started_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),
            status="ended", result="checkmate_win", engine_elo=1500,
            player_color=color, is_rated=True, session_mode="normal",
        )
        db.add(session)
        db.flush()
        board = chess.Board()
        for i, san in enumerate(line):
            fen_before = board.fen()
            board.push(board.parse_san(san))
            db.add(SessionMove(
                session_id=session.id, move_number=i + 1,
                color="white" if i % 2 == 0 else "black",
                move_san=san, fen_before=fen_before, fen_after=board.fen(),
                eval_delta=10,
            ))
    db.commit()


def _seed_scorable_cohort(session_factory) -> None:
    with session_factory() as db:
        _seed_scorable_pair(db, GUARD_USER, "white")
        _seed_scorable_pair(db, GUARD_USER, "black")
        _seed_scorable_pair(db, GUARD_USER, "black", line=CARO_KANN)
        _seed_scorable_pair(db, 2, "white")
        _seed_scorable_pair(db, 3, "white")


@pg_required
def test_self_check_runs_real_scoring_and_publishes(capenv):
    """No stub anywhere: the artifact is frozen, then scored across the full arm grid by
    validate_capture_candidate, then published."""
    _seed_scorable_cohort(capenv.session_factory)
    result = _capture(capenv)

    assert result.release_guard_count == 2
    assert result.quantile_count >= 2          # enough to pool 2 named scores per cell
    assert result.pair_count == result.quantile_count + 2
    # Published, and the committed record describes exactly the published bytes.
    assert hashlib.sha256(capenv.output.read_bytes()).hexdigest() == result.artifact_sha256
    record = json.loads(capenv.provenance.read_bytes())
    assert record["sha256"] == result.artifact_sha256
    assert record["pair_count"] == result.pair_count


@pg_required
def test_self_check_rejects_a_cohort_the_release_path_would_reject(capenv):
    """The producer refuses to publish an artifact the CONSUMER would refuse to load.

    Dropping the black guard's Caro-Kann evidence leaves a cohort that freezes perfectly
    well and fails assert_release_guard_score_shape at load time. Capture must discover
    that BEFORE publication, not leave it for the release run."""
    with capenv.session_factory() as db:
        _seed_scorable_pair(db, GUARD_USER, "white")
        _seed_scorable_pair(db, GUARD_USER, "black")     # no CARO_KANN line
        _seed_scorable_pair(db, 2, "white")
        _seed_scorable_pair(db, 3, "white")

    with pytest.raises(cal.CaptureSelfCheckError) as exc:
        _capture(capenv)
    assert "release-guard" in str(exc.value) or "named_score_map" in str(exc.value)
    # Nothing was published, and no temp survived.
    assert not capenv.output.exists()
    assert not capenv.provenance.exists()
    assert list(capenv.out_dir.glob("*.tmp-*")) == []


# ---------------------------------------------------------------------------
# Mutual exclusion, as INTERLEAVINGS between real processes
# ---------------------------------------------------------------------------

# The unit tests prove the lock helper refuses a busy lock. That is not the acceptance
# criterion: the criterion is about WHEN the losing capture finds out and HOW LONG the
# winner holds on. Both need a second real process, because flock is per open-file-
# description and a second acquisition attempt inside one process proves nothing about
# what another process sees.

_LOCK_PROBE = """
import sys
sys.path.insert(0, {backend!r})
from pathlib import Path
import scripts.calibrate_opening_scores_v2 as cal

common, output, mode = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
try:
    with cal._capture_locks(common, output):
        print("ACQUIRED", flush=True)
        if mode == "hold":
            sys.stdin.readline()   # hold both locks until the parent lets go or kills us
except cal.CaptureLockError:
    print("REFUSED", flush=True)
    raise SystemExit(3)
raise SystemExit(0)
"""


def _spawn_lock_probe(env, mode: str):
    """A REAL second process contending for the same two locks."""
    script = env.out_dir / f"lock_probe_{mode}.py"
    script.write_text(_LOCK_PROBE.format(backend=str(Path(cal.__file__).resolve().parents[1])))
    return subprocess.Popen(
        [sys.executable, str(script), str(env.common), str(env.output.resolve()), mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _await_line(proc, expected: str, timeout: float = 120.0) -> None:
    result: list[str] = []

    def read():
        result.append(proc.stdout.readline())

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(timeout)
    assert result and result[0].strip() == expected, (
        f"probe said {result!r}, expected {expected!r}; stderr={proc.stderr.read()!r}"
        if result else f"probe produced no output within {timeout}s"
    )


@pg_required
def test_a_losing_capture_reads_no_evidence_at_all(capenv, monkeypatch):
    """Two simultaneous captures against the same evidence DB: the loser must find out
    from the LOCK, before it opens a snapshot or touches a single row. A refusal that
    arrived after the reads would still be correct-looking and would still have burned a
    REPEATABLE READ snapshot against production."""
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)

    holder = _spawn_lock_probe(capenv, "hold")
    try:
        _await_line(holder, "ACQUIRED")

        reads: list[str] = []
        for name in ("list_opening_score_candidate_pairs", "capture_freshness_snapshot",
                     "overlay_evidence", "_repeatable_read_snapshot"):
            real = getattr(cal, name)

            def spy(*a, _n=name, _r=real, **kw):
                reads.append(_n)
                return _r(*a, **kw)

            monkeypatch.setattr(cal, name, spy)

        with pytest.raises(cal.CaptureLockError):
            _capture(capenv)

        assert reads == [], f"the losing capture read evidence before refusing: {reads}"
        assert not capenv.output.exists()
        assert not capenv.provenance.exists()
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=60)


@pg_required
def test_the_locks_are_held_across_BOTH_renames(capenv, monkeypatch):
    """The interleaving that a plausible refactor breaks: releasing the locks after the
    ARTIFACT rename instead of after the record rename.

    The winner is paused in the gap between the two os.replace calls — the exact window in
    which the on-disk pair is mismatched — and a real second process is made to contend for
    the locks right there. If the critical section ended at the first rename, the challenger
    would acquire, and a second capture could start publishing over a half-published pair.
    """
    _seed_default_cohort(capenv.session_factory)
    _stub_validate_ok(monkeypatch)

    at_gap = threading.Event()
    resume = threading.Event()
    real_replace = os.replace
    state = {"n": 0}

    def gated_replace(src, dst):
        state["n"] += 1
        out = real_replace(src, dst)
        if state["n"] == 1:              # AFTER the artifact rename, BEFORE the record one
            at_gap.set()
            assert resume.wait(timeout=180), "the test never released the paused capture"
        return out

    monkeypatch.setattr(cal.os, "replace", gated_replace)

    outcome: dict[str, object] = {}

    def run():
        try:
            outcome["result"] = _capture(capenv)
        except BaseException as exc:     # noqa: BLE001 - surfaced by the assertions below
            outcome["error"] = exc

    winner = threading.Thread(target=run, daemon=True)
    winner.start()
    try:
        assert at_gap.wait(timeout=180), f"capture never reached the rename gap: {outcome}"
        # The published-but-unpaired window, observed from outside the process.
        assert capenv.output.exists()
        assert not capenv.provenance.exists()

        challenger = _spawn_lock_probe(capenv, "try")
        _await_line(challenger, "REFUSED")
        assert challenger.wait(timeout=60) == 3
    finally:
        resume.set()
        winner.join(timeout=180)

    assert "error" not in outcome, outcome.get("error")
    assert capenv.provenance.exists()
    assert (json.loads(capenv.provenance.read_bytes())["sha256"]
            == hashlib.sha256(capenv.output.read_bytes()).hexdigest())


@pg_required
def test_a_killed_lock_holder_releases_immediately_with_no_stale_reap(capenv):
    """SIGKILL, not a clean exit: the reason capture never reaps stale locks is that an
    flock dies with the open file description, so a killed holder cannot strand anyone.
    A lock scheme needing a reaper would fail this — and a reaper is exactly the thing that
    lets a live capture be evicted by a nervous operator."""
    holder = _spawn_lock_probe(capenv, "hold")
    _await_line(holder, "ACQUIRED")

    # It really is held: this process cannot take it while the holder lives.
    with pytest.raises(cal.CaptureLockError):
        with cal._capture_locks(str(capenv.common), capenv.output.resolve()):
            pass

    holder.kill()
    assert holder.wait(timeout=60) != 0

    # Immediately reacquirable — no timeout, no PID file, no stale-lock heuristic.
    with cal._capture_locks(str(capenv.common), capenv.output.resolve()):
        pass
    # The lock FILES survive (they are just inodes to flock); only the lock is gone.
    assert (capenv.common / "cohort-capture.lock").exists()


# ---------------------------------------------------------------------------
# The self-check's UNEXPECTED failures are still capture failures
# ---------------------------------------------------------------------------


@pg_required
@pytest.mark.parametrize("boom", [RuntimeError("scorer blew up"),
                                  ValueError("bad grid config"),
                                  KeyError("missing_overlay_node")])
def test_unexpected_self_check_failures_stay_inside_the_typed_boundary(capenv, monkeypatch, boom):
    """The self-check runs a FULL scoring pass, and score_overlay can fail in ways the
    artifact vocabulary does not name. Those are still capture failures: the subcommand
    catches CaptureError and nothing else, so anything else escaping means the command
    prints a traceback instead of the typed diagnostic it promised."""
    _seed_default_cohort(capenv.session_factory)

    def explode(ab, pb, *, graph, roots):
        raise boom

    monkeypatch.setattr(cal, "validate_capture_candidate", explode)

    with pytest.raises(cal.CaptureSelfCheckError) as exc:
        _capture(capenv)
    assert type(boom).__name__ in str(exc.value)
    # Distinguishable from a rejection of the candidate bytes, which is a different verdict.
    assert "FAILED" in str(exc.value)
    # Nothing published, no temp survived.
    assert not capenv.output.exists()
    assert not capenv.provenance.exists()
    assert list(capenv.out_dir.glob("*.tmp-*")) == []


@pg_required
def test_a_rejection_and_a_crash_are_different_diagnostics(capenv, monkeypatch):
    """A FrozenArtifactError means 'these bytes are wrong'; anything else means 'the check
    itself failed'. Collapsing them would tell an operator to go looking at their evidence
    when the real fault is in the scorer."""
    _seed_default_cohort(capenv.session_factory)

    def reject(ab, pb, *, graph, roots):
        raise cal.ArtifactSemanticError("header.pair_count is not an int")

    monkeypatch.setattr(cal, "validate_capture_candidate", reject)
    with pytest.raises(cal.CaptureSelfCheckError) as rejected:
        _capture(capenv)
    assert "rejected the candidate bytes" in str(rejected.value)

    def crash(ab, pb, *, graph, roots):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(cal, "validate_capture_candidate", crash)
    with pytest.raises(cal.CaptureSelfCheckError) as crashed:
        _capture(capenv)
    assert "rejected the candidate bytes" not in str(crashed.value)
    assert "scoring/runtime failure" in str(crashed.value)


@pg_required
def test_the_child_exits_one_without_a_traceback_on_a_self_check_crash(capenv, monkeypatch, capsys):
    """End of the contract: the typed error must actually reach the subcommand's handler."""
    _seed_default_cohort(capenv.session_factory)
    monkeypatch.setenv("GHOSTREPLAY_RELEASE_GUARD_USER", str(GUARD_USER))

    def explode(ab, pb, *, graph, roots):
        raise RuntimeError("scorer blew up")

    monkeypatch.setattr(cal, "validate_capture_candidate", explode)
    rc = cal.main(
        ["capture-cohort", "--output", str(capenv.output)],
        session_factory=capenv.session_factory,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "CaptureSelfCheckError" in err
    assert "Traceback" not in err
