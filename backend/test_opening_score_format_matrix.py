"""Consumer parity across both opening-score storage formats (g-score-store-readers).

Every reader, freshness proof, baseline proof and delta path has to behave the
same whether the pair's marker names legacy per-generation storage or the B50
current row set. The rest of the opening test suite pins legacy-shaped details
(per-generation child rows, keep=2 retention, ON DELETE CASCADE); this module
pins what a CONSUMER is allowed to observe, which must not depend on the format:

- the latest generation's payload, and never a mixture of two;
- valid-empty distinguished from a retired marker;
- the freshness / two-proof baseline / delta verdicts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
import uuid

import pytest

from app import opening_cache as oc
from app.models import EvidenceEpoch, GameSession, OpeningScoreBatch
from app.opening_cache import (
    _is_batch_fresh,
    current_cache_epoch,
    current_evidence_seq,
    _load_batch_shared_scope,
    ensure_tree_cache,
    list_cached_opening_scores,
    list_position_scores,
    lookup_observed_edges_for_parent,
    lookup_observed_edges_for_parents,
    lookup_position_scores,
    lookup_position_scores_for_batch,
    proven_fresh_opening_scores,
    recompute_opening_scores,
    recompute_opening_scores_if_needed,
)
from app.opening_score_delta import (
    BASELINE_RETRYABLE_SOURCES,
    BaselineSnapshotSource,
    _batch_start_mismatch,
    compute_opening_score_delta,
    fill_opening_baselines_for_batch,
    run_baseline_snapshot_job,
)
from app.opening_score_storage import (
    RetiredScoreHandle,
    ScoreHandle,
    handle_is_live,
)

from conftest import TestingSessionLocal

# The seeding fixtures and synthetic graph/roots live with the cache tests; this
# module reuses them rather than maintaining a second copy of the same evidence.
from test_opening_cache import (  # noqa: F401 - _mock_opening_cache_singletons is autouse
    KINGS_PAWN_FEN,
    KINGS_PAWN_FULL,
    KNIGHT_OPENING_FEN,
    OPEN_GAME_FEN,
    START_FEN,
    SYNTHETIC_INITIAL_FEN,
    TWO_KNIGHTS_FEN,
    _make_graph,
    _make_roots,
    _mock_opening_cache_singletons,
    _seed_black_opening_session,
)
# Registered as a plugin rather than imported, so the ``storage_format`` fixture
# is available to every test here without shadowing its own parameter name.
pytest_plugins = ("opening_format_fixture",)

EXPECTED_ROOTS = {KINGS_PAWN_FEN, KNIGHT_OPENING_FEN, SYNTHETIC_INITIAL_FEN}


@pytest.fixture(autouse=True)
def _synthetic_registry_in_the_delta_module():
    """Point the baseline/delta module at the same synthetic graph the cache uses.

    ``_serialize_baseline`` fails closed when the registry fingerprint it derives
    disagrees with the scored batch's, so both modules have to resolve the same
    graph/roots singletons.
    """
    with (
        patch("app.opening_score_delta.get_opening_graph", return_value=_make_graph()),
        patch("app.opening_score_delta.get_opening_roots", return_value=_make_roots()),
    ):
        yield


def _rebuild(db_session):
    result = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert result.batch is not None
    return result.batch


def test_every_repository_reader_agrees_across_formats(db_session, storage_format):
    _seed_black_opening_session(db_session)
    view = _rebuild(db_session)
    assert view.storage_format == storage_format.value

    batch, roots = list_cached_opening_scores(db_session, 123, "black")
    assert batch.id == view.id
    assert {row.opening_key for row in roots} == EXPECTED_ROOTS
    # Display order survives the format-guarded coalesce.
    assert [row.opening_key for row in roots] == [
        row.opening_key
        for row in sorted(roots, key=lambda r: (r.opening_family, r.opening_name))
    ]

    _, positions = list_position_scores(db_session, 123, "black")
    fens = {row.normalized_fen for row in positions}
    assert {START_FEN, KINGS_PAWN_FEN, OPEN_GAME_FEN, KNIGHT_OPENING_FEN} <= fens
    assert TWO_KNIGHTS_FEN not in fens

    # Bounded lookups: a raw clock-bearing FEN normalizes to the same row, and a
    # FEN with no row is simply absent (no-data), not an error.
    _, bounded = lookup_position_scores(
        db_session, 123, "black", [KINGS_PAWN_FULL, TWO_KNIGHTS_FEN]
    )
    assert set(bounded) == {KINGS_PAWN_FEN}
    assert lookup_position_scores_for_batch(
        db_session, view.handle, [KINGS_PAWN_FULL]
    )[KINGS_PAWN_FEN].opening_score is not None

    edges = lookup_observed_edges_for_parent(db_session, view.handle, KINGS_PAWN_FEN)
    assert any(edge.uci == "e7e5" for edge in edges)
    by_parent = lookup_observed_edges_for_parents(
        db_session, view.handle, [KINGS_PAWN_FEN, TWO_KNIGHTS_FEN]
    )
    assert KINGS_PAWN_FEN in by_parent and TWO_KNIGHTS_FEN not in by_parent

    raw_fens, norm_fens = _load_batch_shared_scope(db_session, view.handle)
    assert isinstance(raw_fens, list) and isinstance(norm_fens, list)


def test_only_the_latest_generation_is_ever_visible(db_session, storage_format):
    """Atomic-latest visibility, the format-independent replacement for the legacy
    retention/cascade shape: a second publication is visible whole or not at all."""
    _seed_black_opening_session(db_session)
    first = recompute_opening_scores(db_session, 123, "black")
    first_id, first_generation = first.id, first.generation
    second = recompute_opening_scores(db_session, 123, "black")
    assert second.generation > first_generation

    view, roots = list_cached_opening_scores(db_session, 123, "black")
    assert (view.id, view.generation) == (second.id, second.generation)
    assert {row.opening_key for row in roots} == EXPECTED_ROOTS
    # Exactly one generation's worth of rows — never the union of two.
    assert len(roots) == len(EXPECTED_ROOTS)

    # The superseded marker is an exact handle, and Shape B refuses to serve the
    # current payload under it rather than answering from the newer generation.
    stale = ScoreHandle(
        first_id, 123, "black", first_generation, storage_format
    )
    if not handle_is_live(db_session, stale):
        with pytest.raises(RetiredScoreHandle):
            lookup_position_scores_for_batch(db_session, stale, [KINGS_PAWN_FULL])


def test_the_freshness_proof_holds_in_either_format(db_session, storage_format):
    _seed_black_opening_session(db_session)
    _rebuild(db_session)
    batch, rows, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")
    assert is_fresh is True
    assert _is_batch_fresh(db_session, batch, rows) is True

    # An unstamped signal is still fail-closed through the same code path.
    db_session.get(OpeningScoreBatch, batch.id).evidence_seq = None
    db_session.commit()
    _, _, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")
    assert is_fresh is False


def _active_session(db_session, view):
    """An active session stamped with the durable start watermark proof 2 checks.

    The watermark's registry fingerprint is taken from the batch rather than
    recomputed, because these tests run against the synthetic graph the cache
    fixture installs, not the real opening registry.
    """
    seq = current_evidence_seq(db_session, 123, "black")
    epoch = current_cache_epoch(db_session)
    fingerprint = view.registry_fingerprint
    session = GameSession(
        id=uuid.uuid4(),
        user_id=123,
        started_at=datetime.now(timezone.utc),
        status="active",
        engine_elo=1500,
        player_color="black",
        baseline_watermark_seq=seq,
        baseline_watermark_epoch=epoch,
        baseline_watermark_fingerprint=fingerprint,
    )
    db_session.add(session)
    db_session.commit()
    return session


def test_the_two_proof_baseline_capture_works_in_either_format(
    db_session, storage_format
):
    _seed_black_opening_session(db_session)
    view = _rebuild(db_session)
    session = _active_session(db_session, view)

    assert (
        run_baseline_snapshot_job(db_session, session.id, 123, "black")
        == BaselineSnapshotSource.CACHED_FRESH.value
    )
    db_session.refresh(session)
    assert session.opening_score_baseline is not None
    for key in EXPECTED_ROOTS:
        assert key in session.opening_score_baseline


def test_a_retired_marker_is_retryable_not_a_watermark_mismatch(
    db_session, storage_format
):
    """Proof 2 must raise rather than return a mismatch value.

    ``WATERMARK_MISMATCH`` is terminal, so routing retirement through it would
    permanently drop a session's baseline whenever a rebuild lands between the two
    proofs — precisely the daily-cadence window the retention work depends on.
    """
    _seed_black_opening_session(db_session)
    view = _rebuild(db_session)
    session = _active_session(db_session, view)

    # Proof 2 only reaches its scope queries on epoch drift, so advance the global
    # shared-evidence epoch before retiring the marker under it.
    db_session.execute(
        EvidenceEpoch.__table__.update().values(value=EvidenceEpoch.value + 1)
    )
    db_session.execute(
        OpeningScoreBatch.__table__.delete().where(
            OpeningScoreBatch.__table__.c.id == view.id
        )
    )
    db_session.commit()
    with pytest.raises(RetiredScoreHandle):
        _batch_start_mismatch(db_session, view, session)


def test_retirement_between_the_two_proofs_is_retryable(db_session, storage_format):
    """The baseline job must report a RETRYABLE miss, and capture on the next run.

    ``WATERMARK_MISMATCH`` is terminal, so reporting retirement that way would
    permanently drop a session's baseline whenever a rebuild happens to publish
    between proof 1 and proof 2 — the daily-cadence window the retention work
    depends on.
    """
    _seed_black_opening_session(db_session)
    view = _rebuild(db_session)
    session = _active_session(db_session, view)
    real_is_fresh = oc._is_batch_fresh

    def retire_after_proof_one(db, batch, rows):
        verdict = real_is_fresh(db, batch, rows)
        # A publication lands: the shared epoch moves (so proof 2 reaches its scope
        # queries) and this marker is retired under the reader.
        db.execute(
            EvidenceEpoch.__table__.update().values(value=EvidenceEpoch.value + 1)
        )
        db.execute(
            OpeningScoreBatch.__table__.delete().where(
                OpeningScoreBatch.__table__.c.id == batch.id
            )
        )
        return verdict

    with patch(
        "app.opening_score_delta._is_batch_fresh", side_effect=retire_after_proof_one
    ):
        source = run_baseline_snapshot_job(db_session, session.id, 123, "black")

    assert source == BaselineSnapshotSource.SKIPPED_STALE.value
    assert BaselineSnapshotSource(source) in BASELINE_RETRYABLE_SOURCES
    assert db_session.get(GameSession, session.id).opening_score_baseline is None

    # Retryable means retryable: the very next attempt captures the baseline.
    db_session.rollback()
    assert (
        run_baseline_snapshot_job(db_session, session.id, 123, "black")
        == BaselineSnapshotSource.CACHED_FRESH.value
    )
    assert db_session.get(GameSession, session.id).opening_score_baseline is not None


def test_a_retired_scope_is_stale_not_a_valid_empty_scope(db_session, storage_format):
    """The scope reader must tell "this batch has no shared scope" from "this
    batch is gone". The first keeps today's meaning; the second fails CLOSED."""
    _seed_black_opening_session(db_session)
    view = _rebuild(db_session)
    assert _load_batch_shared_scope(db_session, view.handle) is not None

    db_session.execute(
        EvidenceEpoch.__table__.update().values(value=EvidenceEpoch.value + 1)
    )
    db_session.execute(
        OpeningScoreBatch.__table__.delete().where(
            OpeningScoreBatch.__table__.c.id == view.id
        )
    )
    db_session.commit()

    with pytest.raises(RetiredScoreHandle):
        _load_batch_shared_scope(db_session, view.handle)
    # The freshness gate swallows it and reports stale rather than propagating.
    assert oc._cheap_evidence_fresh(db_session, view) is False


def test_push_fill_and_the_delta_lane_work_in_either_format(
    db_session, storage_format
):
    _seed_black_opening_session(db_session)
    view = _rebuild(db_session)
    session = _active_session(db_session, view)

    assert fill_opening_baselines_for_batch(
        view.id, session_factory=TestingSessionLocal
    ) == 1
    db_session.refresh(session)
    assert session.opening_score_baseline is not None

    # An independent post-publication fill finding its own marker retired is
    # harmless: it discards its work and the ordinary job retries those sessions.
    db_session.execute(
        OpeningScoreBatch.__table__.delete().where(
            OpeningScoreBatch.__table__.c.id == view.id
        )
    )
    db_session.commit()
    assert fill_opening_baselines_for_batch(
        view.id, session_factory=TestingSessionLocal
    ) == 0


def test_the_terminal_delta_reads_scores_in_either_format(db_session, storage_format):
    game = _seed_black_opening_session(db_session)
    _rebuild(db_session)
    items = compute_opening_score_delta(db_session, game)
    # Every played root reports an "after" read straight from the cached batch.
    assert items
    assert all(item.after is not None for item in items if item.opening_key in EXPECTED_ROOTS)


def test_the_tree_cache_resolver_serves_either_format(db_session, storage_format):
    _seed_black_opening_session(db_session)
    view = _rebuild(db_session)
    graph, roots = _make_graph(), _make_roots()
    batch_id, computed_at, state, registry = ensure_tree_cache(
        db_session, 123, "black", graph, roots
    )
    assert (batch_id, state) == (view.id, "warm_fresh")
    assert registry == oc.opening_score_inputs_fingerprint(
        graph, roots, oc.load_strict_densified_edges(graph)
    )
    assert computed_at is not None
