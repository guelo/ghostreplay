"""Tests for end-of-session opening-score deltas (g-xanz).

Covers the shared helper (snapshot_opening_baseline + compute_opening_score_delta)
and the endpoint wiring that surfaces ``opening_score_changes`` on game end, drill
natural-end, drill accuracy-fail, and the off-route route-check failure path.
"""

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import chess
import pytest
from sqlalchemy import text

import app.opening_evidence as opening_evidence
from conftest import TestingSessionLocal

from app.models import (
    AnalysisCache,
    AnalysisCacheSubmission,
    GameSession,
    OpeningScoreBatch,
    PositionAnalysisRow,
    SessionMove,
    User,
    UserOpeningScore,
)
from app.opening_baseline_scheduler import OpeningBaselineScheduler
from app.opening_score_scheduler import OpeningScoreScheduler, OpeningScoreTrigger
from app.opening_cache import (
    OpeningScoreRecomputeResult,
    RecomputeDisposition,
    SCORE_MODEL_VERSION,
    bump_evidence_seq,
    capture_freshness_snapshot,
    current_cache_epoch,
    current_evidence_seq,
    opening_score_inputs_fingerprint,
)
from app.opening_graph import (
    OpeningGraph,
    OpeningGraphNode,
    _fen_from_board,
    get_opening_graph,
)
from app.opening_roots import OpeningRoot, OpeningRoots, get_opening_roots
from app.opening_rootcalc import RootCalcConfig, root_calc_config_fingerprint
from app.opening_score_delta import (
    OPENING_BASELINE_SCHEMA_VERSION,
    _current_scoped_delta,
    _parse_compatible_baseline,
    compute_opening_score_delta,
    publish_scoped_opening_score_deltas,
    read_opening_score_delta,
    reserve_scoped_delta_generation,
    run_baseline_snapshot_job,
    snapshot_opening_baseline,
)
from app.opening_score_delta_lane import OpeningScoreDeltaLane


@contextmanager
def _injected_baseline_scheduler():
    """Bind a non-autostart ``OpeningBaselineScheduler`` into both /start handler
    aliases so a test can drive async baseline capture deterministically with
    ``run_due()`` (overrides the autouse ``_no_op_baseline_enqueue`` for this block).
    """
    sched = OpeningBaselineScheduler(
        session_factory=TestingSessionLocal, auto_start=False
    )

    def _enqueue(session_id, user_id, player_color):
        sched.enqueue(session_id, user_id, player_color)

    with (
        patch("app.api.game.enqueue_baseline_snapshot", _enqueue),
        patch("app.api.drills.enqueue_baseline_snapshot", _enqueue),
    ):
        yield sched

# The helper imports get_opening_roots / load_cached_rows into its own namespace,
# and lazy-imports the scheduler funcs from app.opening_score_scheduler.
PATCH_ROOTS = "app.opening_score_delta.get_opening_roots"


@pytest.fixture(autouse=True)
def _stub_scheduler():
    """Stub the scheduler so no worker thread spawns and seeded batches are never
    recomputed away. Neither the start-path snapshot (g-fix-start-latency) nor the
    end-path delta (g-fix-end-latency) calls refresh_now anymore, but the end-path
    compute now enqueues a BACKGROUND request_recompute — stub it to a no-op so no
    real worker fires (and so tests can assert it WAS enqueued).

    ``is_recompute_scheduled`` is the cheap NOT-fresh gate the poll consults
    (g-xmhv); default it to False (quiescent) so freshness tests reach
    ``_is_batch_fresh`` deterministically regardless of cross-test scheduler state.
    ``is_recompute_inflight`` is the narrower IN-FLIGHT-ONLY gate the one-shot
    baseline snapshot consults (g-1iul); default it to False so the existing
    quiescent snapshot tests still reach ``_is_batch_fresh``. Tests exercising the
    scheduled/in-flight branches override these locally."""
    with (
        patch("app.opening_score_scheduler.request_recompute", return_value=None),
        patch("app.opening_score_scheduler.refresh_now", return_value=True),
        patch("app.opening_score_scheduler.is_recompute_scheduled", return_value=False),
        patch("app.opening_score_scheduler.is_recompute_inflight", return_value=False),
    ):
        yield


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _baseline_envelope(
    scores: dict[str, float],
    *,
    model_version: str = SCORE_MODEL_VERSION,
    config_fingerprint: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": OPENING_BASELINE_SCHEMA_VERSION,
        "model_version": model_version,
        "root_calc_config_fingerprint": (
            root_calc_config_fingerprint()
            if config_fingerprint is None
            else config_fingerprint
        ),
        "scores": scores,
    }


def _baseline_json(
    scores: dict[str, float],
    *,
    model_version: str = SCORE_MODEL_VERSION,
    config_fingerprint: str | None = None,
) -> str:
    return json.dumps(
        _baseline_envelope(
            scores,
            model_version=model_version,
            config_fingerprint=config_fingerprint,
        )
    )


@pytest.mark.parametrize("scores", [{}, {"a": 0.0, "b": 42.5, "c": 100.0}])
def test_baseline_parser_accepts_same_model_envelopes(scores):
    assert _parse_compatible_baseline(_baseline_json(scores)) == scores


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "not-json",
        "[]",
        "{}",
        json.dumps({"legacy": 42.0}),
        json.dumps(
            {
                "schema_version": True,
                "model_version": SCORE_MODEL_VERSION,
                "root_calc_config_fingerprint": root_calc_config_fingerprint(),
                "scores": {},
            }
        ),
        json.dumps(
            {
                "schema_version": 2,
                "model_version": SCORE_MODEL_VERSION,
                "root_calc_config_fingerprint": root_calc_config_fingerprint(),
                "scores": {},
            }
        ),
        _baseline_json({}, model_version="sm-v2-3"),
        json.dumps(
            {
                "schema_version": OPENING_BASELINE_SCHEMA_VERSION,
                "model_version": SCORE_MODEL_VERSION,
                "root_calc_config_fingerprint": root_calc_config_fingerprint(),
                "scores": [],
            }
        ),
        _baseline_json({"bad": True}),
        _baseline_json({"bad": -0.1}),
        _baseline_json({"bad": 100.1}),
        _baseline_json({"bad": float("nan")}),
        _baseline_json({"bad": float("inf")}),
        _baseline_json({"bad": 10**400}),
    ],
)
def test_baseline_parser_suppresses_legacy_cross_model_and_invalid_payloads(payload):
    assert _parse_compatible_baseline(payload) is None


def test_baseline_parser_suppresses_same_model_different_root_config():
    old_config_fingerprint = root_calc_config_fingerprint(
        RootCalcConfig(report_fold_p=0.75)
    )
    assert old_config_fingerprint != root_calc_config_fingerprint()

    payload = _baseline_json(
        {"a": 42.0},
        model_version=SCORE_MODEL_VERSION,
        config_fingerprint=old_config_fingerprint,
    )
    assert _parse_compatible_baseline(payload) is None


def _fens_after(sans: list[str]) -> list[str]:
    board = chess.Board()
    out: list[str] = []
    for san in sans:
        board.push_san(san)
        out.append(_fen_from_board(board))
    return out


def _full_fens_after(sans: list[str]) -> list[str]:
    board = chess.Board()
    out: list[str] = []
    for san in sans:
        board.push_san(san)
        out.append(board.fen())
    return out


def _insert_moves(db_session, session_id: str, sans: list[str]) -> None:
    for index, (san, fen_after) in enumerate(zip(sans, _full_fens_after(sans))):
        db_session.add(
            SessionMove(
                session_id=uuid.UUID(str(session_id)),
                move_number=index // 2 + 1,
                color="white" if index % 2 == 0 else "black",
                move_san=san,
                fen_after=fen_after,
                segment="normal",
            )
        )
    db_session.commit()


def _make_roots(specs: dict[str, dict]) -> OpeningRoots:
    roots: dict[str, OpeningRoot] = {}
    child_map: dict[str, set[str]] = {key: set() for key in specs}
    for key, spec in specs.items():
        for parent in spec.get("parents", []):
            child_map[parent].add(key)
    for key, spec in specs.items():
        roots[key] = OpeningRoot(
            opening_key=key,
            opening_name=spec["name"],
            opening_family=spec["family"],
            eco=spec.get("eco"),
            depth=spec["depth"],
            parent_keys=frozenset(spec.get("parents", [])),
            child_keys=frozenset(child_map[key]),
        )
    ownership = {key: frozenset({key}) for key in specs}
    return OpeningRoots(roots, ownership)


RUY_SANS = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]
_RUY_FENS = _fens_after(RUY_SANS)
KP_KEY = _RUY_FENS[0]       # after 1. e4
RUY_KEY = _RUY_FENS[4]      # after 3. Bb5
MORPHY_KEY = _RUY_FENS[5]   # after 3... a6


def _ruy_roots() -> OpeningRoots:
    return _make_roots({
        KP_KEY: {"name": "King's Pawn Game", "family": "King's Pawn Game", "eco": "B00", "depth": 1, "parents": []},
        RUY_KEY: {"name": "Ruy Lopez", "family": "Ruy Lopez", "eco": "C60", "depth": 5, "parents": [KP_KEY]},
        MORPHY_KEY: {"name": "Ruy Lopez: Morphy Defense", "family": "Ruy Lopez", "eco": "C70", "depth": 6, "parents": [RUY_KEY]},
    })


def _ruy_graph() -> OpeningGraph:
    board = chess.Board()
    nodes: dict[str, OpeningGraphNode] = {}
    parent = _fen_from_board(board)
    nodes[parent] = OpeningGraphNode(parent, "white")
    for san in [*RUY_SANS, "Ba4"]:
        move = board.parse_san(san)
        uci = move.uci()
        board.push(move)
        child = _fen_from_board(board)
        nodes.setdefault(
            child,
            OpeningGraphNode(child, "white" if board.turn else "black"),
        )
        nodes[parent].children[uci] = child
        nodes[child].parents.add((parent, uci))
        parent = child
    graph = OpeningGraph(nodes, _fen_from_board(chess.Board()))
    graph.freeze()
    return graph


@contextmanager
def _patched_delta_registry(graph: OpeningGraph, roots: OpeningRoots):
    """Bind scorer, publisher, and batch validator to one synthetic registry."""
    with (
        patch("app.opening_score_delta.get_opening_graph", return_value=graph),
        patch("app.opening_score_delta.get_opening_roots", return_value=roots),
        patch("app.opening_cache.get_opening_graph", return_value=graph),
        patch("app.opening_cache.get_opening_roots", return_value=roots),
    ):
        yield


def _insert_scoped_evidence(db_session, session_id) -> None:
    board = chess.Board()
    for index, san in enumerate([*RUY_SANS, "Ba4"]):
        fen_before = board.fen()
        color = "white" if board.turn else "black"
        board.push_san(san)
        db_session.add(
            SessionMove(
                session_id=uuid.UUID(str(session_id)),
                move_number=index // 2 + 1,
                color=color,
                move_san=san,
                fen_before=fen_before,
                fen_after=board.fen(),
                eval_delta=20 if color == "white" else None,
                segment="normal",
            )
        )
    db_session.commit()


def _make_fresh_batch_for_registry(
    db_session,
    graph: OpeningGraph,
    roots: OpeningRoots,
    *,
    generation: int = 1,
) -> int:
    snapshot = capture_freshness_snapshot(db_session, 123, "white")
    batch = OpeningScoreBatch(
        user_id=123,
        player_color="white",
        generation=generation,
        registry_fingerprint=opening_score_inputs_fingerprint(graph, roots),
        inputs_fingerprint=snapshot.inputs_fingerprint,
        evidence_seq=snapshot.evidence_seq,
        cache_epoch=snapshot.cache_epoch,
        scoped_shared_digest=snapshot.scoped_shared_digest,
        computed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    db_session.add(batch)
    db_session.flush()
    return batch.id


def _make_batch(db_session, *, user_id=123, player_color="white", generation=1,
                fresh=True) -> int:
    """Seed a batch. ``fresh=True`` stamps the registry fingerprint AND the full
    g-jact freshness bundle (inputs fingerprint, evidence_seq, cache_epoch,
    scoped shared digest) the freshness gate (_is_batch_fresh) checks, so the
    snapshot treats it as provably current (test users carry no evidence -> a
    deterministic empty bundle). ``fresh=False`` leaves the signal NULL
    (legacy/stale) so the gate skips it (source=skipped_stale)."""
    if fresh:
        registry_fp = opening_score_inputs_fingerprint(
            get_opening_graph(), get_opening_roots()
        )
        snap = capture_freshness_snapshot(db_session, user_id, player_color)
        batch = OpeningScoreBatch(
            user_id=user_id, player_color=player_color, generation=generation,
            registry_fingerprint=registry_fp,
            inputs_fingerprint=snap.inputs_fingerprint,
            evidence_seq=snap.evidence_seq,
            cache_epoch=snap.cache_epoch,
            scoped_shared_digest=snap.scoped_shared_digest,
            computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
    else:
        batch = OpeningScoreBatch(
            user_id=user_id, player_color=player_color, generation=generation,
            registry_fingerprint="fp",
            inputs_fingerprint=None,
            computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
    db_session.add(batch)
    db_session.flush()
    return batch.id


def _add_score_row(db_session, *, batch_id, opening_key, opening_score,
                   user_id=123, player_color="white", opening_name="x",
                   opening_family="x"):
    db_session.add(UserOpeningScore(
        batch_id=batch_id, user_id=user_id, player_color=player_color,
        opening_key=opening_key, opening_name=opening_name,
        opening_family=opening_family, opening_score=opening_score,
        confidence=0.5, coverage=0.5, weighted_depth=1.0, sample_size=5,
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    ))


def _make_session(
    db_session,
    *,
    user_id=123,
    player_color="white",
    baseline=None,
    status="ended",
    session_mode="normal",
    drill_state=None,
    drill_terminal_reason=None,
    started_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
) -> GameSession:
    sid = uuid.uuid4()
    db_session.add(GameSession(
        id=sid, user_id=user_id,
        started_at=started_at,
        status=status,
        result="checkmate_win" if status == "ended" else None,
        engine_elo=1500,
        player_color=player_color,
        is_rated=session_mode == "normal",
        session_mode=session_mode,
        drill_state=drill_state,
        drill_terminal_reason=drill_terminal_reason,
        opening_score_baseline=baseline,
    ))
    db_session.commit()
    return db_session.query(GameSession).filter(GameSession.id == sid).one()


def _seed_evidence(db_session, *, user_id=123, player_color="white") -> None:
    """Create a normal-mode session move with fen_before so has_opening_evidence
    sees the user as having opening evidence (a candidate pair) even with no batch
    — the cold-cache-with-evidence case the start path must skip, not empty-baseline."""
    sid = uuid.uuid4()
    db_session.add(GameSession(
        id=sid, user_id=user_id,
        started_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        status="ended", result="checkmate_win", engine_elo=1500,
        player_color=player_color, session_mode="normal",
    ))
    db_session.add(SessionMove(
        session_id=sid, move_number=1, color="white", move_san="e4",
        fen_before="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        fen_after="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        segment="normal",
    ))
    db_session.commit()


# ---------------------------------------------------------------------------
# snapshot_opening_baseline
# ---------------------------------------------------------------------------

def test_snapshot_returns_json_score_map(db_session):
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    _add_score_row(db_session, batch_id=batch_id, opening_key=MORPHY_KEY, opening_score=75.0)
    db_session.commit()

    snap = snapshot_opening_baseline(db_session, 123, "white")
    assert json.loads(snap) == _baseline_envelope(
        {RUY_KEY: 41.0, MORPHY_KEY: 75.0}
    )


def test_snapshot_empty_map_when_no_batch_no_evidence(db_session):
    # No batch AND no evidence -> a brand-new user -> a valid empty envelope,
    # NOT None, so the session's first openings later read as new rather than unknown.
    assert snapshot_opening_baseline(db_session, 123, "white") == _baseline_json({})


def test_snapshot_skips_cold_cache_with_evidence(db_session):
    # No batch yet but the user DOES have evidence (cold cache, e.g. post-restart
    # first read): can't prove a baseline. Returning "{}" would falsely mark every
    # existing opening "new" at session end, so skip (NULL) instead.
    _seed_evidence(db_session, user_id=123, player_color="white")
    assert snapshot_opening_baseline(db_session, 123, "white") is None


def test_snapshot_skips_when_batch_stale(db_session):
    # A batch exists but its fingerprints don't match current inputs (legacy/stale,
    # NO scheduler activity needed to detect it). Snapshotting it would persist a
    # stale "before" -> end-of-session misattribution. Skip (NULL baseline).
    batch_id = _make_batch(db_session, fresh=False)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()

    assert snapshot_opening_baseline(db_session, 123, "white") is None


def test_snapshot_does_not_call_refresh_now(db_session):
    # The start hot path must never touch the scheduler. A fresh batch yields the
    # score map WITHOUT a single refresh_now call (no 5s timeout exposure).
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()

    with patch("app.opening_score_scheduler.refresh_now", new=Mock()) as mock_refresh:
        snap = snapshot_opening_baseline(db_session, 123, "white")

    assert json.loads(snap) == _baseline_envelope({RUY_KEY: 41.0})
    mock_refresh.assert_not_called()


def test_snapshot_logs_source_signal(db_session, caplog):
    # Observability lands WITH the fix: source + snapshot_ms are in the message
    # string (root formatter prints %(message)s only), so the latency win is provable.
    import logging

    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()
    with caplog.at_level(logging.INFO, logger="app.opening_score_delta"):
        snapshot_opening_baseline(db_session, 123, "white")
    assert "source=cached_fresh" in caplog.text
    assert "snapshot_ms=" in caplog.text

    caplog.clear()
    _make_batch(db_session, user_id=999, player_color="white", fresh=False)
    db_session.commit()
    with caplog.at_level(logging.INFO, logger="app.opening_score_delta"):
        snapshot_opening_baseline(db_session, 999, "white")
    assert "source=skipped_stale" in caplog.text


def test_snapshot_none_on_failure(db_session):
    # list_cached_opening_scores is the FIRST DB call in the snapshot try block and
    # always runs (in-flight or quiescent), so injecting there reliably reaches the
    # except handler regardless of the gate branch.
    with patch(
        "app.opening_score_delta.list_cached_opening_scores",
        side_effect=RuntimeError("boom"),
    ):
        assert snapshot_opening_baseline(db_session, 123, "white") is None


def test_snapshot_rolls_back_on_failure(db_session):
    # A failed read can abort the transaction (Postgres); snapshot must roll back
    # so the caller's session-create commit is not poisoned.
    with (
        patch(
            "app.opening_score_delta.list_cached_opening_scores",
            side_effect=RuntimeError("boom"),
        ),
        patch.object(db_session, "rollback") as mock_rollback,
    ):
        result = snapshot_opening_baseline(db_session, 123, "white")
    assert result is None
    mock_rollback.assert_called_once()


# --- g-1iul: in-flight-only cheap gate for the one-shot baseline snapshot -----


def test_snapshot_inflight_with_batch_skips_digest_and_evidence(db_session, caplog):
    # The 9.6s regression: while a recompute is IN-FLIGHT and a batch exists, the
    # snapshot must NOT pay the O(evidence) freshness digest (it would serialize
    # GIL-bound against the running worker) NOR probe has_opening_evidence. It
    # degrades to NULL with source=skipped_recompute_inflight. Spying on both is
    # the load-bearing check.
    import logging

    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()

    spy_fresh = Mock(
        side_effect=AssertionError("digest must not run while recompute in-flight")
    )
    spy_evidence = Mock(
        side_effect=AssertionError("evidence probe must not run when batch present")
    )
    with (
        caplog.at_level(logging.INFO, logger="app.opening_score_delta"),
        patch("app.opening_score_scheduler.is_recompute_inflight", return_value=True),
        patch("app.opening_score_delta._is_batch_fresh", new=spy_fresh),
        patch("app.opening_score_delta.has_opening_evidence", new=spy_evidence),
    ):
        result = snapshot_opening_baseline(db_session, 123, "white")

    assert result is None
    spy_fresh.assert_not_called()
    spy_evidence.assert_not_called()
    assert "source=skipped_recompute_inflight" in caplog.text


def test_snapshot_delta_lane_inflight_with_batch_skips_digest(db_session, caplog):
    """The synchronous baseline guard recognizes either opening-score worker."""
    import logging

    batch_id = _make_batch(db_session)
    _add_score_row(
        db_session,
        batch_id=batch_id,
        opening_key=RUY_KEY,
        opening_score=41.0,
    )
    db_session.commit()

    spy_fresh = Mock(
        side_effect=AssertionError("digest must not run while delta lane is in-flight")
    )
    with (
        caplog.at_level(logging.INFO, logger="app.opening_score_delta"),
        patch("app.opening_score_scheduler.is_recompute_inflight", return_value=False),
        patch("app.opening_score_delta_lane.is_delta_lane_inflight", return_value=True),
        patch("app.opening_score_delta._is_batch_fresh", new=spy_fresh),
    ):
        result = snapshot_opening_baseline(db_session, 123, "white")

    assert result is None
    spy_fresh.assert_not_called()
    assert "source=skipped_recompute_inflight" in caplog.text


def test_snapshot_inflight_no_batch_with_evidence_skips(db_session, caplog):
    # In-flight, no batch, but the user HAS evidence (cold-with-evidence during a
    # recompute): can't prove a baseline -> NULL, source=skipped_recompute_inflight.
    # The digest still never runs.
    import logging

    _seed_evidence(db_session, user_id=123, player_color="white")

    spy_fresh = Mock(side_effect=AssertionError("digest must not run in-flight"))
    with (
        caplog.at_level(logging.INFO, logger="app.opening_score_delta"),
        patch("app.opening_score_scheduler.is_recompute_inflight", return_value=True),
        patch("app.opening_score_delta._is_batch_fresh", new=spy_fresh),
    ):
        result = snapshot_opening_baseline(db_session, 123, "white")

    assert result is None
    spy_fresh.assert_not_called()
    assert "source=skipped_recompute_inflight" in caplog.text


def test_snapshot_inflight_no_batch_no_evidence_empty(db_session, caplog):
    # In-flight, no batch, no evidence (brand-new user): still a valid empty
    # baseline so the session's first openings read as new, NOT skipped.
    import logging

    spy_fresh = Mock(side_effect=AssertionError("digest must not run in-flight"))
    with (
        caplog.at_level(logging.INFO, logger="app.opening_score_delta"),
        patch("app.opening_score_scheduler.is_recompute_inflight", return_value=True),
        patch("app.opening_score_delta._is_batch_fresh", new=spy_fresh),
    ):
        result = snapshot_opening_baseline(db_session, 123, "white")

    assert json.loads(result) == _baseline_envelope({})
    spy_fresh.assert_not_called()
    assert "source=empty_no_evidence" in caplog.text


def test_snapshot_quiescent_with_batch_still_proves_freshness(db_session):
    # The in-flight gate is IN-FLIGHT-ONLY: a quiescent scheduler (default stub
    # False) must STILL reach the digest and capture the confident baseline. A
    # pending-but-not-running recompute is deliberately not gated here.
    from app import opening_score_delta as osd

    batch_id = _make_batch(db_session)  # fresh fingerprints
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()

    spy_fresh = Mock(wraps=osd._is_batch_fresh)
    with patch.object(osd, "_is_batch_fresh", new=spy_fresh):
        snap = snapshot_opening_baseline(db_session, 123, "white")

    spy_fresh.assert_called_once()
    assert json.loads(snap) == _baseline_envelope({RUY_KEY: 41.0})


def test_delta_does_not_call_refresh_now(db_session):
    # g-fix-end-latency: the end path must NEVER block on the scheduler. A warm
    # batch yields the delta WITHOUT a single refresh_now call (no 5s timeout
    # exposure on the terminal action).
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    with (
        patch("app.opening_score_scheduler.refresh_now", new=Mock()) as mock_refresh,
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        items = compute_opening_score_delta(db_session, session)

    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].after == pytest.approx(44.0)
    assert by_key[RUY_KEY].delta == pytest.approx(3.0)
    mock_refresh.assert_not_called()


def test_delta_enqueues_immediate_scoped_and_debounced_full_recompute(db_session):
    # The terminal helper submits the immediate scoped lane first, then preserves
    # the ordinary debounced whole-graph request for dashboard/tree convergence.
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    with (
        patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta", new=Mock()
        ) as mock_scoped,
        patch(
            "app.opening_score_scheduler.request_recompute", new=Mock()
        ) as mock_full,
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        compute_opening_score_delta(db_session, session)

    mock_scoped.assert_called_once_with(123, "white", session.id)
    mock_full.assert_called_once_with(
        123,
        "white",
        source=OpeningScoreTrigger.SCORE_DELTA,
    )


@pytest.mark.parametrize("failed_submission", ["scoped", "full"])
def test_delta_submission_failures_are_independent_and_keep_cached_items(
    db_session, failed_submission
):
    session = _make_session(
        db_session, baseline=_baseline_json({RUY_KEY: 41.0})
    )
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(
        db_session,
        batch_id=batch_id,
        opening_key=RUY_KEY,
        opening_score=44.0,
    )
    db_session.commit()

    scoped = Mock()
    full = Mock()
    {"scoped": scoped, "full": full}[failed_submission].side_effect = RuntimeError(
        "submission failed"
    )
    with (
        patch("app.opening_score_delta_lane.enqueue_scoped_delta", scoped),
        patch("app.opening_score_scheduler.request_recompute", full),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        items = compute_opening_score_delta(db_session, session)

    assert {item.opening_key: item for item in items}[RUY_KEY].after == pytest.approx(
        44.0
    )
    scoped.assert_called_once_with(123, "white", session.id)
    full.assert_called_once_with(
        123,
        "white",
        source=OpeningScoreTrigger.SCORE_DELTA,
    )


def test_delta_terminal_post_never_proves_freshness(db_session):
    # g-xmhv: the terminal POST must serve the warm banner WITHOUT the O(evidence)
    # freshness proof (raw_evidence_inputs_digest), which was the residual ~9s cost.
    # Patch the raw-input fingerprint to fail-if-called; the warm delta is still
    # built from list_cached_opening_scores (digest is OFF the terminal path).
    # compute swallows exceptions, so assert_not_called() is the load-bearing check.
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)  # seeds fingerprints BEFORE the patch
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    mock_fp = Mock(
        side_effect=AssertionError("freshness proof must not run on terminal POST")
    )
    with (
        patch("app.opening_cache.opening_score_raw_inputs_fingerprint", new=mock_fp),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        items = compute_opening_score_delta(db_session, session)

    mock_fp.assert_not_called()
    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].after == pytest.approx(44.0)
    assert by_key[RUY_KEY].delta == pytest.approx(3.0)


def test_delta_empty_when_cold_no_batch(db_session):
    # Cold cache (an opening was crossed but no batch exists yet): the compute
    # returns NO items rather than an all-None banner — the poll fills it in once
    # the background recompute builds the first batch.
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_moves(db_session, session.id, RUY_SANS)

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        assert compute_opening_score_delta(db_session, session) == []


def test_delta_logs_source_and_compute_ms(db_session, caplog):
    # Observability: source + compute_ms are in the message string (root formatter
    # prints %(message)s only), so production api_request duration can verify the fix.
    import logging

    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    with (
        caplog.at_level(logging.INFO, logger="app.opening_score_delta"),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        compute_opening_score_delta(db_session, session)
    # g-xmhv: the terminal POST serves the warm banner UNVERIFIED (no freshness
    # proof), so it logs source=cached_unverified, not cached_fresh.
    assert "source=cached_unverified" in caplog.text
    assert "compute_ms=" in caplog.text


def test_legacy_baseline_suppresses_terminal_delta_and_poll_keeps_freshness(
    db_session, caplog
):
    import logging

    session = _make_session(
        db_session,
        baseline=json.dumps({RUY_KEY: 41.0}),  # pre-sm-v2-4 bare map
    )
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(
        db_session,
        batch_id=batch_id,
        opening_key=RUY_KEY,
        opening_score=44.0,
    )
    db_session.commit()

    with (
        caplog.at_level(logging.INFO, logger="app.opening_score_delta"),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        terminal = compute_opening_score_delta(db_session, session)
        polled, is_fresh = read_opening_score_delta(db_session, session)

    for items in (terminal, polled):
        item = {candidate.opening_key: candidate for candidate in items}[RUY_KEY]
        assert item.before is None
        assert item.after == pytest.approx(44.0)
        assert item.delta is None
        assert item.is_new is False
    assert is_fresh is True
    assert "source=cached_suppressed" in caplog.text


def test_baseline_job_swallows_capture_failure(db_session):
    # Re-homed from the synchronous /start path (g-mxeo): a capture failure inside
    # the worker job never raises, leaves the baseline NULL, and does not crash.
    session = _make_session(
        db_session, status="active",
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    with patch(
        "app.opening_score_delta.list_cached_opening_scores",
        side_effect=RuntimeError("db boom"),
    ):
        source = run_baseline_snapshot_job(
            db_session, session.id, session.user_id, session.player_color
        )
    assert source == "failed"
    db_session.expire_all()
    refreshed = db_session.query(GameSession).filter(
        GameSession.id == session.id
    ).one()
    assert refreshed.opening_score_baseline is None


def test_game_start_returns_201_with_null_when_enqueue_best_effort(
    client, auth_headers, db_session
):
    # Endpoint contract: /start returns 201 with a NULL baseline immediately —
    # capture is async and best-effort, so an enqueue no-op/failure never regresses
    # the response (autouse _no_op_baseline_enqueue stubs the enqueue to a no-op).
    resp = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    assert resp.status_code == 201
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(resp.json()["session_id"])
    ).one()
    assert session.opening_score_baseline is None


def test_game_start_does_not_block_on_scheduler(client, auth_headers, db_session):
    # Endpoint-level regression: the baseline is NULL immediately (async capture),
    # the drained worker populates it from a fresh cache, and NO refresh_now runs.
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()

    with patch("app.opening_score_scheduler.refresh_now", new=Mock()) as mock_refresh:
        with _injected_baseline_scheduler() as sched:
            resp = client.post(
                "/api/game/start",
                json={"engine_elo": 1500, "player_color": "white"},
                headers=auth_headers(user_id=123),
            )
            assert resp.status_code == 201
            sid = uuid.UUID(resp.json()["session_id"])
            immediate = db_session.query(GameSession).filter(
                GameSession.id == sid
            ).one()
            assert immediate.opening_score_baseline is None
            sched.run_due()

    db_session.expire_all()
    session = db_session.query(GameSession).filter(GameSession.id == sid).one()
    import json
    assert json.loads(session.opening_score_baseline) == _baseline_envelope(
        {RUY_KEY: 41.0}
    )
    mock_refresh.assert_not_called()


def test_drill_start_does_not_block_on_scheduler(client, auth_headers, db_session):
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=KP_KEY, opening_score=33.0)
    db_session.commit()

    with patch("app.opening_score_scheduler.refresh_now", new=Mock()) as mock_refresh:
        with _injected_baseline_scheduler() as sched:
            start = _start_drill(client, auth_headers)
            assert start.status_code == 201
            sid = uuid.UUID(start.json()["session_id"])
            immediate = db_session.query(GameSession).filter(
                GameSession.id == sid
            ).one()
            assert immediate.opening_score_baseline is None
            sched.run_due()

    db_session.expire_all()
    session = db_session.query(GameSession).filter(GameSession.id == sid).one()
    import json
    assert json.loads(session.opening_score_baseline) == _baseline_envelope(
        {KP_KEY: 33.0}
    )
    mock_refresh.assert_not_called()


# ---------------------------------------------------------------------------
# compute_opening_score_delta
# ---------------------------------------------------------------------------

def test_delta_numeric_when_baseline_has_key(db_session):
    session = _make_session(
        db_session,
        baseline=_baseline_json({RUY_KEY: 41.0, MORPHY_KEY: 75.0}),
    )
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    _add_score_row(db_session, batch_id=batch_id, opening_key=MORPHY_KEY, opening_score=80.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items = compute_opening_score_delta(db_session, session)

    by_key = {item.opening_key: item for item in items}
    assert [i.opening_key for i in items] == [KP_KEY, RUY_KEY, MORPHY_KEY]
    # RUY: present in baseline and batch -> numeric delta.
    assert by_key[RUY_KEY].before == pytest.approx(41.0)
    assert by_key[RUY_KEY].after == pytest.approx(44.0)
    assert by_key[RUY_KEY].delta == pytest.approx(3.0)
    assert by_key[RUY_KEY].is_new is False
    assert by_key[MORPHY_KEY].delta == pytest.approx(5.0)
    # KP: missing from baseline AND batch -> brand-new opening, no after score.
    assert by_key[KP_KEY].is_new is True
    assert by_key[KP_KEY].before is None
    assert by_key[KP_KEY].after is None
    assert by_key[KP_KEY].delta is None


def test_delta_is_new_when_baseline_lacks_key(db_session):
    # Empty baseline ("{}") -> every crossed opening is new; after-scores shown,
    # no numeric delta.
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=30.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items = compute_opening_score_delta(db_session, session)

    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].is_new is True
    assert by_key[RUY_KEY].before is None
    assert by_key[RUY_KEY].after == pytest.approx(30.0)
    assert by_key[RUY_KEY].delta is None


def test_delta_null_baseline_shows_after_only(db_session):
    # Pre-feature session (baseline NULL): can't claim "new"; show after, no delta.
    session = _make_session(db_session, baseline=None)
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=50.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items = compute_opening_score_delta(db_session, session)

    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].is_new is False
    assert by_key[RUY_KEY].before is None
    assert by_key[RUY_KEY].after == pytest.approx(50.0)
    assert by_key[RUY_KEY].delta is None


def test_delta_empty_when_no_opening_crossed(db_session):
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_moves(db_session, session.id, RUY_SANS)
    # Registry has only an unrelated root the game never reaches.
    other = _make_roots({
        "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -": {
            "name": "Queen's Pawn", "family": "Queen's Pawn", "depth": 1, "parents": []
        }
    })
    with patch(PATCH_ROOTS, return_value=other):
        assert compute_opening_score_delta(db_session, session) == []


def test_delta_after_none_when_opening_unscored(db_session):
    # Opening crossed but no cached row yet (after unknown) and baseline empty.
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_moves(db_session, session.id, RUY_SANS)
    _make_batch(db_session)  # empty batch, no rows
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items = compute_opening_score_delta(db_session, session)

    for item in items:
        assert item.after is None
        assert item.delta is None
        assert item.is_new is True


def test_delta_never_raises_on_internal_failure(db_session):
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_moves(db_session, session.id, RUY_SANS)
    with patch(PATCH_ROOTS, side_effect=RuntimeError("boom")):
        assert compute_opening_score_delta(db_session, session) == []


# ---------------------------------------------------------------------------
# read_opening_score_delta (GET reconcile-poll reader — non-blocking)
# ---------------------------------------------------------------------------

def test_read_delta_fresh_returns_items_and_true(db_session):
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)  # fresh fingerprints
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items, is_fresh = read_opening_score_delta(db_session, session)

    assert is_fresh is True
    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].after == pytest.approx(44.0)
    assert by_key[RUY_KEY].delta == pytest.approx(3.0)


def test_read_delta_stale_returns_items_and_false(db_session):
    # Items are served for ANY warm batch; is_fresh only drives the poll-stop signal.
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session, fresh=False)  # stale fingerprints
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items, is_fresh = read_opening_score_delta(db_session, session)

    assert is_fresh is False
    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].after == pytest.approx(44.0)


def test_read_delta_cold_returns_empty_and_false(db_session):
    # No batch yet but an opening crossed -> keep polling (False), no all-None banner.
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_moves(db_session, session.id, RUY_SANS)
    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items, is_fresh = read_opening_score_delta(db_session, session)
    assert items == []
    assert is_fresh is False


def test_batch_cold_scoped_publication_returns_fresh_after_scores(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        items, is_fresh = read_opening_score_delta(db_session, session)

    assert is_fresh is True
    by_key = {item.opening_key: item for item in items}
    assert by_key[KP_KEY].after is not None
    assert by_key[RUY_KEY].after is not None
    assert by_key[MORPHY_KEY].after is not None
    assert all(item.is_new for item in items)


_REPLAY_CACHE_FIELDS = (
    "replay_cache_builds",
    "replay_cache_probed_sessions",
    "replay_cache_l1_hits",
    "replay_cache_l2_hits",
    "replay_cache_raw_derivations",
    "replay_cache_persisted_upserts",
    "replay_cache_l2_read_failed",
    "replay_cache_l2_write_failed",
)


def _publish_scoped_report(db_session, session_id) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    request = reserve_scoped_delta_generation(session_id)
    assert publish_scoped_opening_score_deltas(
        db_session,
        123,
        "white",
        (request,),
        on_complete=reports.append,
    ) == 1
    assert len(reports) == 1
    return reports[0]


def _replay_cache_report(report: dict[str, object]) -> dict[str, object]:
    return {field: report[field] for field in _REPLAY_CACHE_FIELDS}


def test_scoped_completion_reports_cold_process_l2_hydration(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)

    with _patched_delta_registry(graph, roots):
        opening_evidence.overlay_evidence(db_session, 123, "white", graph)
        db_session.commit()  # SQLite L2 is durable with the caller transaction.
        opening_evidence.reset_session_evidence_cache()
        report = _publish_scoped_report(db_session, session.id)

    assert _replay_cache_report(report) == {
        "replay_cache_builds": 1,
        "replay_cache_probed_sessions": 1,
        "replay_cache_l1_hits": 0,
        "replay_cache_l2_hits": 1,
        "replay_cache_raw_derivations": 0,
        "replay_cache_persisted_upserts": 0,
        "replay_cache_l2_read_failed": False,
        "replay_cache_l2_write_failed": False,
    }


def test_scoped_completion_reports_incremental_raw_fallback(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    changed = _make_session(db_session, baseline=_baseline_json({}))
    unchanged = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, changed.id)
    _insert_scoped_evidence(db_session, unchanged.id)

    with _patched_delta_registry(graph, roots):
        opening_evidence.overlay_evidence(db_session, 123, "white", graph)
        db_session.commit()
        move = (
            db_session.query(SessionMove)
            .filter(SessionMove.session_id == changed.id)
            .order_by(SessionMove.move_number, SessionMove.color)
            .first()
        )
        assert move is not None
        move.eval_delta = 99
        db_session.commit()
        report = _publish_scoped_report(db_session, changed.id)

    assert _replay_cache_report(report) == {
        "replay_cache_builds": 1,
        "replay_cache_probed_sessions": 2,
        "replay_cache_l1_hits": 1,
        "replay_cache_l2_hits": 0,
        "replay_cache_raw_derivations": 1,
        "replay_cache_persisted_upserts": 1,
        "replay_cache_l2_read_failed": False,
        "replay_cache_l2_write_failed": False,
    }


def test_scoped_completion_reports_l2_adapter_failures(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)
    db_session.execute(text("DROP TABLE opening_session_replay_cache"))

    with _patched_delta_registry(graph, roots):
        report = _publish_scoped_report(db_session, session.id)

    assert _replay_cache_report(report) == {
        "replay_cache_builds": 1,
        "replay_cache_probed_sessions": 1,
        "replay_cache_l1_hits": 0,
        "replay_cache_l2_hits": 0,
        "replay_cache_raw_derivations": 1,
        "replay_cache_persisted_upserts": 0,
        "replay_cache_l2_read_failed": True,
        "replay_cache_l2_write_failed": True,
    }


@pytest.mark.parametrize(
    ("session_mode", "status", "drill_state", "drill_terminal_reason"),
    [
        ("normal", "ended", None, None),
        ("drill", "active", "failed", "accuracy"),
    ],
)
def test_scoped_poll_is_fresh_while_full_recompute_remains_inflight(
    db_session,
    session_mode,
    status,
    drill_state,
    drill_terminal_reason,
):
    """Normal and drill polls can read publication before the full writer returns."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(
        db_session,
        baseline=_baseline_json({}),
        session_mode=session_mode,
        status=status,
        drill_state=drill_state,
        drill_terminal_reason=drill_terminal_reason,
    )
    _insert_scoped_evidence(db_session, session.id)
    full_entered = threading.Event()
    release_full = threading.Event()

    def blocked_full(_db, _user_id, _player_color):
        full_entered.set()
        assert release_full.wait(timeout=5.0)
        return OpeningScoreRecomputeResult(
            disposition=RecomputeDisposition.NO_EVIDENCE,
            batch=None,
        )

    scheduler = OpeningScoreScheduler(
        session_factory=TestingSessionLocal,
        recompute=blocked_full,
        quiet_window=0.0,
        auto_start=False,
    )
    scheduler.request_recompute(
        123,
        "white",
        source=OpeningScoreTrigger.SCORE_DELTA,
    )
    lane = OpeningScoreDeltaLane(
        session_factory=TestingSessionLocal,
        publish=publish_scoped_opening_score_deltas,
        auto_start=False,
    )
    lane.enqueue(123, "white", session.id)
    worker = threading.Thread(
        target=scheduler.run_due,
        kwargs={"now": float("inf")},
    )

    with _patched_delta_registry(graph, roots):
        worker.start()
        try:
            assert full_entered.wait(timeout=5.0)
            assert scheduler.is_inflight(123, "white") is True
            lane.run_due()
            with TestingSessionLocal() as reader_db:
                authoritative = reader_db.get(GameSession, session.id)
                items, is_fresh = read_opening_score_delta(
                    reader_db, authoritative
                )
            assert items
            assert is_fresh is True
            assert scheduler.is_inflight(123, "white") is True
        finally:
            release_full.set()
            worker.join(timeout=5.0)

    assert worker.is_alive() is False


def test_fresh_batch_precedes_scoped_publication(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 5.0}))
    _insert_scoped_evidence(db_session, session.id)

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        scoped_score = dict(_current_scoped_delta(session.id).scored_roots)[RUY_KEY]
        batch_id = _make_fresh_batch_for_registry(db_session, graph, roots)
        _add_score_row(
            db_session,
            batch_id=batch_id,
            opening_key=RUY_KEY,
            opening_score=scoped_score + 7.0,
        )
        db_session.commit()

        items, is_fresh = read_opening_score_delta(db_session, session)

    assert is_fresh is True
    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].after == pytest.approx(scoped_score + 7.0)


def test_scoped_publication_rejects_generation_drift(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1

        # A newer enqueue invalidates the older completion before it runs.
        reserve_scoped_delta_generation(session.id)
        assert read_opening_score_delta(db_session, session) == ([], False)


def test_scoped_publication_rejects_new_per_user_fallback_scope(
    client,
    auth_headers,
    db_session,
):
    """A real eligible /moves upload invalidates before the old scope can rehash."""
    auth_headers(user_id=123)  # Seed the token's backing user row.
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        publication = _current_scoped_delta(session.id)
        assert publication is not None

        # Upload a second, already-ended game's legitimate Queen's Gambit line.
        # Its third ply contributes a player-colour fallback candidate at a FEN
        # absent from the stored Ruy scope.  The production /moves path performs
        # the matching evidence_seq bump in the same transaction.
        added_session = _make_session(db_session, baseline=_baseline_json({}))
        board = chess.Board()
        moves = []
        for index, san in enumerate(("d4", "d5", "c4")):
            fen_before = board.fen()
            move = board.parse_san(san)
            board.push(move)
            moves.append(
                {
                    "move_number": index // 2 + 1,
                    "color": "white" if index % 2 == 0 else "black",
                    "move_san": san,
                    "move_uci": move.uci(),
                    "fen_before": fen_before,
                    "fen_after": board.fen(),
                    "eval_delta": 20 if index % 2 == 0 else None,
                }
            )
        added_fallback_fen = moves[2]["fen_before"]
        assert added_fallback_fen not in publication.shared_raw_fens

        # The deferred evidence worker is orthogonal here; keep the mutation at
        # the endpoint's durable per-user session-move + cursor transaction.
        with patch("app.api.session.enqueue_session_evidence"):
            response = client.post(
                f"/api/session/{added_session.id}/moves",
                json={"moves": moves, "recompute_opportunity": False},
                headers=auth_headers(user_id=123),
            )
        assert response.status_code == 200, response.text
        assert response.json()["moves_inserted"] == 3
        assert (
            current_evidence_seq(db_session, 123, "white")
            > publication.evidence_seq
        )

        # Sequence mismatch is checked before epoch/scoped-digest recovery, so
        # the old stored scope cannot accept the newly introduced candidate.
        with patch(
            "app.opening_score_delta.shared_scope_digest",
            side_effect=AssertionError("sequence drift must reject before rehash"),
        ) as scoped_digest:
            assert read_opening_score_delta(db_session, session) == ([], False)
        scoped_digest.assert_not_called()


def test_scoped_epoch_drift_rehashes_scope_and_rearms(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        before = _current_scoped_delta(session.id)
        assert before is not None
        db_session.execute(
            text("UPDATE evidence_epoch SET value = value + 1 WHERE id = 1")
        )
        db_session.commit()
        live_epoch = current_cache_epoch(db_session)
        assert live_epoch != before.cache_epoch

        items, is_fresh = read_opening_score_delta(db_session, session)

    assert items and is_fresh is True
    assert _current_scoped_delta(session.id).cache_epoch == live_epoch


@pytest.mark.parametrize(
    "surface",
    ("analysis_cache", "position_analysis", "analysis_cache_submission"),
)
def test_scoped_epoch_drift_rejects_real_in_scope_shared_mutation(
    db_session,
    surface,
):
    graph = _ruy_graph()
    roots = _ruy_roots()
    if db_session.get(User, 123) is None:
        db_session.add(User(id=123, username=None, is_anonymous=True))
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)
    raw_fen = chess.Board().fen()
    norm_fen = _fen_from_board(chess.Board())
    cache_row = AnalysisCache(
        fen_before=raw_fen,
        normalized_fen_before=norm_fen,
        move_uci="e2e4",
        move_san="e4",
        source="game",
    )
    db_session.add(cache_row)
    position_row = None
    if surface == "position_analysis":
        position_row = PositionAnalysisRow(
            normalized_fen=norm_fen,
            fen=raw_fen,
            best_move_uci="e2e4",
            source="precomputed",
        )
        db_session.add(position_row)
    db_session.commit()

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        publication = _current_scoped_delta(session.id)
        assert publication is not None
        assert raw_fen in publication.shared_raw_fens
        assert norm_fen in publication.shared_norm_fens

        if surface == "analysis_cache":
            cache_row.classification = "good"
        elif surface == "position_analysis":
            assert position_row is not None
            position_row.best_move_uci = "d2d4"
        else:
            db_session.add(
                AnalysisCacheSubmission(
                    analysis_cache_id=cache_row.id,
                    user_id=123,
                )
            )
        db_session.commit()
        assert current_cache_epoch(db_session) > publication.cache_epoch

        assert read_opening_score_delta(db_session, session) == ([], False)


def test_scoped_publication_rejects_missing_epoch_and_registry_drift(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        with patch(
            "app.opening_score_delta.opening_score_inputs_fingerprint",
            return_value="registry-drift",
        ):
            assert read_opening_score_delta(db_session, session) == ([], False)

        db_session.execute(text("DELETE FROM evidence_epoch WHERE id = 1"))
        db_session.commit()
        assert read_opening_score_delta(db_session, session) == ([], False)


def test_scoped_publication_cache_loss_is_a_safe_miss(db_session):
    from app.opening_score_delta import reset_scoped_delta_cache

    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        reset_scoped_delta_cache()
        assert read_opening_score_delta(db_session, session) == ([], False)


def test_scoped_build_discards_counter_drift_and_avoids_full_snapshot(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    session_id = session.id
    _insert_scoped_evidence(db_session, session.id)
    reports: list[dict[str, object]] = []

    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta.current_evidence_seq",
            side_effect=[4, 5],
        ),
        patch(
            "app.opening_cache.capture_freshness_snapshot",
            side_effect=AssertionError("full freshness snapshot is forbidden"),
        ) as full_snapshot,
        patch(
            "app.opening_evidence.raw_evidence_inputs_snapshot",
            side_effect=AssertionError("raw digest is forbidden"),
        ) as raw_snapshot,
    ):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session,
            123,
            "white",
            (request,),
            on_complete=reports.append,
        ) == 0

    assert db_session.in_transaction() is False
    assert _current_scoped_delta(session_id) is None
    assert reports[0]["outcome"] == "counter_drift"
    assert _replay_cache_report(reports[0]) == {
        "replay_cache_builds": 1,
        "replay_cache_probed_sessions": 1,
        "replay_cache_l1_hits": 0,
        "replay_cache_l2_hits": 0,
        "replay_cache_raw_derivations": 1,
        "replay_cache_persisted_upserts": 1,
        "replay_cache_l2_read_failed": False,
        "replay_cache_l2_write_failed": False,
    }
    full_snapshot.assert_not_called()
    raw_snapshot.assert_not_called()


def test_scoped_build_rolls_back_read_transaction_when_no_candidate(db_session):
    request = reserve_scoped_delta_generation(uuid.uuid4())

    assert publish_scoped_opening_score_deltas(
        db_session, 123, "white", (request,)
    ) == 0
    assert db_session.in_transaction() is False


def test_scoped_publication_rejects_authoritative_state_change(db_session):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        session.status = "active"
        session.result = None
        db_session.commit()
        assert read_opening_score_delta(db_session, session) == ([], False)


def test_back_to_back_active_session_does_not_invalidate_until_terminal(
    db_session,
):
    graph = _ruy_graph()
    roots = _ruy_roots()
    first = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, first.id)

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(first.id)
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1

        second = _make_session(
            db_session, baseline=_baseline_json({}), status="active"
        )
        _insert_scoped_evidence(db_session, second.id)
        assert read_opening_score_delta(db_session, first)[1] is True

        second.status = "ended"
        second.result = "checkmate_win"
        bump_evidence_seq(db_session, 123, "white")
        db_session.commit()
        assert read_opening_score_delta(db_session, first) == ([], False)


def test_read_delta_no_chain_returns_empty_and_true(db_session):
    # No opening crossed -> nothing will ever appear, so stop the poll (True).
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_moves(db_session, session.id, RUY_SANS)
    other = _make_roots({
        "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -": {
            "name": "Queen's Pawn", "family": "Queen's Pawn", "depth": 1, "parents": []
        }
    })
    with patch(PATCH_ROOTS, return_value=other):
        items, is_fresh = read_opening_score_delta(db_session, session)
    assert items == []
    assert is_fresh is True


def test_read_delta_never_touches_scheduler(db_session):
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    with (
        patch("app.opening_score_scheduler.refresh_now", new=Mock()) as mock_refresh,
        patch(
            "app.opening_score_scheduler.request_recompute", new=Mock()
        ) as mock_enqueue,
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        read_opening_score_delta(db_session, session)

    mock_refresh.assert_not_called()
    mock_enqueue.assert_not_called()


def test_read_delta_fresh_batch_ignores_recompute_scheduled(db_session):
    # g-delta-commit-decouple: scheduler state is observability, not freshness.
    # A provably-fresh batch remains fresh while another run is pending/in-flight;
    # the cheap signal still never calls the O(evidence) raw digest.
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    mock_fp = Mock(
        side_effect=AssertionError("digest must not run while recompute scheduled")
    )
    with (
        patch(
            "app.opening_score_scheduler.is_recompute_scheduled", return_value=True
        ),
        patch("app.opening_cache.opening_score_raw_inputs_fingerprint", new=mock_fp),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        items, is_fresh = read_opening_score_delta(db_session, session)

    assert is_fresh is True
    mock_fp.assert_not_called()
    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].after == pytest.approx(44.0)


def test_read_delta_proves_freshness_cheaply_when_quiescent(db_session):
    # g-xmhv: a quiescent scheduler is the ONLY path that proves freshness.
    # g-jact: that proof is now the cheap partitioned signal — the O(evidence)
    # raw digest/fingerprint must NOT run on the verdict path, yet is_fresh is
    # still asserted True for a genuinely current batch.
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)  # fresh fingerprints + signal
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    fp_spy = Mock(
        side_effect=AssertionError("raw fingerprint must not run on the cheap verdict path")
    )
    digest_spy = Mock(
        side_effect=AssertionError("raw digest must not run on the cheap verdict path")
    )
    with (
        patch(
            "app.opening_score_scheduler.is_recompute_scheduled", return_value=False
        ),
        patch("app.opening_cache.opening_score_raw_inputs_fingerprint", new=fp_spy),
        patch("app.opening_cache.raw_evidence_inputs_snapshot", new=digest_spy),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        items, is_fresh = read_opening_score_delta(db_session, session)

    fp_spy.assert_not_called()
    digest_spy.assert_not_called()
    assert is_fresh is True
    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].delta == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Endpoint wiring: game start populates baseline, game end returns deltas
# ---------------------------------------------------------------------------

def test_game_start_populates_baseline(client, auth_headers, db_session):
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()

    with _injected_baseline_scheduler() as sched:
        resp = client.post(
            "/api/game/start",
            json={"engine_elo": 1500, "player_color": "white"},
            headers=auth_headers(user_id=123),
        )
        assert resp.status_code == 201
        sid = uuid.UUID(resp.json()["session_id"])
        # Immediate value is NULL — capture is async (g-mxeo).
        immediate = db_session.query(GameSession).filter(GameSession.id == sid).one()
        assert immediate.opening_score_baseline is None
        sched.run_due()

    db_session.expire_all()
    session = db_session.query(GameSession).filter(GameSession.id == sid).one()
    import json
    assert json.loads(session.opening_score_baseline) == _baseline_envelope(
        {RUY_KEY: 41.0}
    )


def test_game_end_returns_opening_score_changes(client, auth_headers, db_session):
    start = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.opening_score_baseline = _baseline_json(
        {RUY_KEY: 41.0, MORPHY_KEY: 75.0}
    )
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)

    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    _add_score_row(db_session, batch_id=batch_id, opening_key=MORPHY_KEY, opening_score=80.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4", "is_rated": False},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[RUY_KEY]["delta"] == pytest.approx(3.0)
    assert changes[MORPHY_KEY]["delta"] == pytest.approx(5.0)
    assert changes[KP_KEY]["is_new"] is True


# ---------------------------------------------------------------------------
# Endpoint wiring: drill terminal paths
# ---------------------------------------------------------------------------

DRILL_ROOT_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"


def _drill_roots() -> OpeningRoots:
    root = OpeningRoot(
        opening_key=DRILL_ROOT_FEN, opening_name="King's Pawn Game",
        opening_family="King's Pawn Game", eco="B00", depth=1,
        parent_keys=frozenset(), child_keys=frozenset(),
    )
    return OpeningRoots({DRILL_ROOT_FEN: root}, {DRILL_ROOT_FEN: frozenset([DRILL_ROOT_FEN])})


def _start_drill(client, auth_headers, *, user_id=123):
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        return client.post(
            "/api/drills/start",
            json={"opening_key": DRILL_ROOT_FEN, "player_color": "white",
                  "engine_elo": 1500, "strictness": "standard"},
            headers=auth_headers(user_id=user_id),
        )


def _seed_drill_after_scores(db_session):
    batch_id = _make_batch(db_session, player_color="white")
    _add_score_row(db_session, batch_id=batch_id, opening_key=KP_KEY, opening_score=60.0)
    db_session.commit()


def test_drill_start_populates_baseline(client, auth_headers, db_session):
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=KP_KEY, opening_score=33.0)
    db_session.commit()

    with _injected_baseline_scheduler() as sched:
        start = _start_drill(client, auth_headers)
        assert start.status_code == 201
        sid = uuid.UUID(start.json()["session_id"])
        immediate = db_session.query(GameSession).filter(GameSession.id == sid).one()
        assert immediate.opening_score_baseline is None
        sched.run_due()

    db_session.expire_all()
    session = db_session.query(GameSession).filter(GameSession.id == sid).one()
    import json
    assert json.loads(session.opening_score_baseline) == _baseline_envelope(
        {KP_KEY: 33.0}
    )


def test_drill_natural_end_returns_opening_score_changes(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.opening_score_baseline = _baseline_json({KP_KEY: 40.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    _seed_drill_after_scores(db_session)

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.post(
            f"/api/drills/{session_id}/natural-end",
            json={"result": "checkmate_win", "pgn": "1. e4"},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[KP_KEY]["before"] == pytest.approx(40.0)
    assert changes[KP_KEY]["after"] == pytest.approx(60.0)
    assert changes[KP_KEY]["delta"] == pytest.approx(20.0)


def test_drill_accuracy_fail_returns_opening_score_changes(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.drill_state = "root_reached"
    session.opening_score_baseline = _baseline_json({KP_KEY: 40.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    _seed_drill_after_scores(db_session)

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.post(
            f"/api/drills/{session_id}/fail",
            json={"terminal_reason": "accuracy"},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[KP_KEY]["delta"] == pytest.approx(20.0)


def test_drill_endpoints_omit_changes_when_not_terminal(client, auth_headers, db_session):
    # start / get must NOT carry deltas (no recompute on those paths).
    start = _start_drill(client, auth_headers)
    assert start.json()["opening_score_changes"] is None
    session_id = start.json()["session_id"]
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        got = client.get(f"/api/drills/{session_id}", headers=auth_headers(user_id=123))
    assert got.json()["opening_score_changes"] is None


# --- off-route route-check failure path -----------------------------------

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"
D4_FEN = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -"


def _steering_graph() -> OpeningGraph:
    nodes = {
        START_FEN: OpeningGraphNode(START_FEN, "white"),
        E4_FEN: OpeningGraphNode(E4_FEN, "black"),
        E4_E5_FEN: OpeningGraphNode(E4_E5_FEN, "white"),
    }
    nodes[START_FEN].children["e2e4"] = E4_FEN
    nodes[E4_FEN].parents.add((START_FEN, "e2e4"))
    nodes[E4_FEN].children["e7e5"] = E4_E5_FEN
    nodes[E4_E5_FEN].parents.add((E4_FEN, "e7e5"))
    graph = OpeningGraph(nodes, START_FEN)
    graph.freeze()
    return graph


def test_drill_offroute_route_check_omits_opening_score_changes(
    client, auth_headers, db_session
):
    # Off-route fail no longer carries a delta: route-check is a speculative
    # per-move call that can't be upload-barriered, and going off-route means the
    # target opening was never reached. The response must not expose the field.
    graph = _steering_graph()
    offroute_roots = _make_roots({
        E4_E5_FEN: {"name": "Open Game", "family": "Open Game", "depth": 2, "parents": []},
    })
    with (
        patch("app.api.drills.get_opening_roots", return_value=offroute_roots),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={"opening_key": E4_E5_FEN, "player_color": "white",
                  "engine_elo": 1500, "strictness": "standard"},
            headers=auth_headers(user_id=123),
        )
        assert start.status_code == 201
        session_id = start.json()["session_id"]

        session = db_session.query(GameSession).filter(
            GameSession.id == uuid.UUID(session_id)
        ).one()
        session.opening_score_baseline = _baseline_json({KP_KEY: 40.0})
        db_session.commit()
        _insert_moves(db_session, session_id, RUY_SANS)
        _seed_drill_after_scores(db_session)

        with patch(PATCH_ROOTS, return_value=_ruy_roots()):
            resp = client.post(
                f"/api/drills/{session_id}/route-check",
                json={"current_fen": D4_FEN, "previous_fen": START_FEN,
                      "played_uci": "d2d4"},
                headers=auth_headers(user_id=123),
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert "opening_score_changes" not in data


def test_route_check_on_route_omits_opening_score_changes(client, auth_headers, db_session):
    # The hot (non-terminal) route-check branch must not carry deltas either.
    graph = _steering_graph()
    roots = _make_roots({
        E4_E5_FEN: {"name": "Open Game", "family": "Open Game", "depth": 2, "parents": []},
    })
    with (
        patch("app.api.drills.get_opening_roots", return_value=roots),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={"opening_key": E4_E5_FEN, "player_color": "white",
                  "engine_elo": 1500, "strictness": "standard"},
            headers=auth_headers(user_id=123),
        )
        session_id = start.json()["session_id"]
        # Playing the on-route first move (1. e4) stays on route.
        resp = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": E4_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(user_id=123),
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "on_route"
    assert "opening_score_changes" not in resp.json()


# --- abandon gate (P3) ----------------------------------------------------

def test_game_end_abandon_skips_opening_score_changes(client, auth_headers, db_session):
    start = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.opening_score_baseline = _baseline_json({RUY_KEY: 41.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    # Even with baseline + moves + scores that would otherwise yield a delta, an
    # abandon end must skip the (synchronous) recompute entirely.
    with patch(
        "app.api.game.compute_opening_score_delta"
    ) as mock_compute:
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "abandon", "pgn": "1. e4", "is_rated": False},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    assert resp.json()["opening_score_changes"] is None
    mock_compute.assert_not_called()


# --- terminal endpoints never block on the scheduler (g-fix-end-latency) ----
#
# refresh_now is patched with an AssertionError side-effect AND asserted
# not-called: compute swallows exceptions, so the side-effect alone wouldn't
# surface a regression — the assert_not_called() is the load-bearing check.


def test_game_end_does_not_block_on_scheduler(client, auth_headers, db_session):
    start = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.opening_score_baseline = _baseline_json({RUY_KEY: 41.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    mock_refresh = Mock(side_effect=AssertionError("refresh_now must not run"))
    with (
        patch("app.opening_score_scheduler.refresh_now", new=mock_refresh),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4", "is_rated": False},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    mock_refresh.assert_not_called()
    # The warm delta is still served (non-blocking, not degraded).
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[RUY_KEY]["delta"] == pytest.approx(3.0)


def test_game_end_never_proves_freshness(client, auth_headers, db_session):
    # g-xmhv headline regression: /api/game/end returns 200 with the warm banner
    # while BOTH refresh_now AND the O(evidence) digest are fail-if-called — proving
    # the residual 9.95s freshness proof is OFF the terminal POST path. compute
    # swallows exceptions, so the assert_not_called() pair is the load-bearing check.
    start = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.opening_score_baseline = _baseline_json({RUY_KEY: 41.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    batch_id = _make_batch(db_session)  # seeds fingerprints BEFORE the patch
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    mock_refresh = Mock(side_effect=AssertionError("refresh_now must not run"))
    mock_fp = Mock(
        side_effect=AssertionError("freshness digest must not run on terminal POST")
    )
    with (
        patch("app.opening_score_scheduler.refresh_now", new=mock_refresh),
        patch("app.opening_cache.opening_score_raw_inputs_fingerprint", new=mock_fp),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4", "is_rated": False},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    mock_refresh.assert_not_called()
    mock_fp.assert_not_called()
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[RUY_KEY]["delta"] == pytest.approx(3.0)


def test_drill_natural_end_does_not_block_on_scheduler(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.opening_score_baseline = _baseline_json({KP_KEY: 40.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    _seed_drill_after_scores(db_session)

    mock_refresh = Mock(side_effect=AssertionError("refresh_now must not run"))
    with (
        patch("app.opening_score_scheduler.refresh_now", new=mock_refresh),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        resp = client.post(
            f"/api/drills/{session_id}/natural-end",
            json={"result": "checkmate_win", "pgn": "1. e4"},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    mock_refresh.assert_not_called()
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[KP_KEY]["delta"] == pytest.approx(20.0)


def test_drill_accuracy_fail_does_not_block_on_scheduler(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.drill_state = "root_reached"
    session.opening_score_baseline = _baseline_json({KP_KEY: 40.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    _seed_drill_after_scores(db_session)

    mock_refresh = Mock(side_effect=AssertionError("refresh_now must not run"))
    with (
        patch("app.opening_score_scheduler.refresh_now", new=mock_refresh),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        resp = client.post(
            f"/api/drills/{session_id}/fail",
            json={"terminal_reason": "accuracy"},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    mock_refresh.assert_not_called()
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[KP_KEY]["delta"] == pytest.approx(20.0)


# --- GET /api/openings/score-delta/{session_id} reconcile-poll --------------

def test_get_score_delta_returns_fresh_changes(client, auth_headers, db_session):
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.get(
            f"/api/openings/score-delta/{session.id}",
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_fresh"] is True
    changes = {c["opening_key"]: c for c in body["opening_score_changes"]}
    assert changes[RUY_KEY]["delta"] == pytest.approx(3.0)


def test_get_score_delta_cold_is_not_fresh_with_no_changes(client, auth_headers, db_session):
    # Cold cache, opening crossed: is_fresh False (keep polling), no banner yet.
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_moves(db_session, session.id, RUY_SANS)
    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.get(
            f"/api/openings/score-delta/{session.id}",
            headers=auth_headers(user_id=123),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_fresh"] is False
    assert body["opening_score_changes"] is None


def test_get_score_delta_unknown_session_404(client, auth_headers):
    resp = client.get(
        f"/api/openings/score-delta/{uuid.uuid4()}",
        headers=auth_headers(user_id=123),
    )
    assert resp.status_code == 404


def test_get_score_delta_wrong_owner_403(client, auth_headers, db_session):
    session = _make_session(db_session, user_id=123, baseline=_baseline_json({}))
    resp = client.get(
        f"/api/openings/score-delta/{session.id}",
        headers=auth_headers(user_id=999),
    )
    assert resp.status_code == 403


def test_get_score_delta_never_blocks_on_scheduler(client, auth_headers, db_session):
    session = _make_session(db_session, baseline=_baseline_json({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    with (
        patch("app.opening_score_scheduler.refresh_now", new=Mock()) as mock_refresh,
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        resp = client.get(
            f"/api/openings/score-delta/{session.id}",
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    mock_refresh.assert_not_called()
