"""Deterministic reproduction of the stale terminal poll after a scoped publication
(g-delta-stale-publish).

The g-6qwoj pilot captured ~12 s accuracy-failure polls whose *own* scoped lane run
had already published in well under a second. Production logs could not name the
reader's rejection branch: ``read_opening_score_delta`` logs only ``source=`` and
``compute_ms=``, and every scoped miss degrades to the same stale fallback.

These tests pin the sequence on the checked-out revision and separate the two ways a
terminal poll ends up waiting on the whole-graph worker instead of the ~800 ms scoped
lane:

* **Post-publication invalidation** — the deferred ``/moves`` side effects run AFTER
  the terminal publication, write an analysis-cache row at a position this session
  played, and bump the global cache epoch. The row is inside the publication's own
  shared scope, so the epoch re-arm in ``_validated_scoped_score_map`` fails its digest
  guard and the publication is rejected for good.
* **Safe-discard with no retry** — ``publish_scoped_opening_score_deltas`` finishes
  ``counter_drift`` (a correct discard), returns normally with zero publications, and
  ``OpeningScoreDeltaLane._run_one`` requeues only on *exceptions*, so nothing reruns.

Both landed on the same gap: the terminal branch of ``read_opening_score_delta``
returned the stale fallback without the recovery re-enqueue its active-boundary sibling
performs, so neither shape had any path back to the lane. The fix closes exactly that —
``enqueue_terminal_delta_recovery`` asks for one bounded replacement run. Every freshness
guard here is asserted as CORRECT and none is relaxed: what the tests below pin is that a
correct rejection is now followed by a bounded retry instead of a ten-second wait.
"""

import logging
import uuid
from unittest.mock import patch

import chess
import pytest
from sqlalchemy import text

from app.api.session import (
    SessionMoveInput,
    _run_session_move_evidence_side_effects,
)
from app.game_phase import Division
from app.session_contracts import utcnow
from app.models import AnalysisCache, GameSession, User
from app.opening_cache import (
    bump_evidence_seq,
    current_cache_epoch,
    current_evidence_seq,
)
from app.opening_evidence import session_is_evidence_eligible, shared_scope_digest
from app.opening_score_delta import (
    _current_scoped_delta,
    active_prefix_snapshot,
    publish_scoped_opening_score_deltas,
    read_opening_score_delta,
    reserve_scoped_delta_generation,
)
from conftest import TestingSessionLocal

from app.opening_score_delta_lane import (
    DeltaLaneEnqueueOutcome,
    OpeningScoreDeltaLane,
)

# The synthetic Ruy registry + seeding helpers are shared with the main delta suite;
# duplicating them here would let the two reproductions drift apart.
from test_opening_score_delta import (  # noqa: F401 — _stub_scheduler is autouse
    KP_KEY,
    RUY_SANS,
    _add_score_row,
    _baseline_json,
    _insert_scoped_evidence,
    _make_batch,
    _make_fresh_batch_for_registry,
    _make_session,
    _patched_delta_registry,
    _ruy_graph,
    _ruy_roots,
    _stub_scheduler,
)

# A valid browser-game-v2 claim. Without one the shared writer refuses the row
# (``INACTIVE_PROFILE_KEEP``, g-bgv1-cutover) and the deferred run writes nothing —
# which is itself a captured production shape, covered by the control test below.
_BUILD = "a8fbc05ec6920b56d7485826dcb02c5ffd2826bcbf751cf973046f237a9096f1"
_NET = (
    "nn-9067e33176e8.nnue:"
    "9067e33176e8c5edb7aa8db6a3aedd012f84a1f39872e86357c6c2d0993f314d"
)


def _provenance() -> dict[str, object]:
    return {
        "engine_version": "18",
        "engine_build": _BUILD,
        "eval_file_id": _NET,
        "search_limit_type": "depth",
        "search_limit_value": 17,
        "threads": 1,
        "hash_mb": 128,
    }


def _final_upload_moves(*, with_provenance: bool) -> list[SessionMoveInput]:
    """The final ``/moves`` payload for exactly the line ``_insert_scoped_evidence``
    seeded, so every analysis-cache row the deferred run writes lands on a position
    this session actually played."""
    board = chess.Board()
    moves: list[SessionMoveInput] = []
    for index, san in enumerate([*RUY_SANS, "Ba4"]):
        fen_before = board.fen()
        color = "white" if board.turn else "black"
        move = board.parse_san(san)
        uci = move.uci()
        board.push(move)
        moves.append(
            SessionMoveInput(
                move_number=index // 2 + 1,
                color=color,
                move_san=san,
                fen_before=fen_before,
                fen_after=board.fen(),
                move_uci=uci,
                eval_cp=20,
                best_move_san=san,
                best_move_uci=uci,
                best_move_eval_cp=20,
                eval_delta=0,
                classification="best",
                provenance=_provenance() if with_provenance else None,
            )
        )
    return moves


def _run_deferred_final_upload(db_session, session_id, *, with_provenance: bool) -> None:
    """Run the real deferred ``/moves`` worker body — the production function the
    evidence scheduler invokes on its own thread after the upload has committed."""
    _run_session_move_evidence_side_effects(
        db_session,
        session_id=uuid.UUID(str(session_id)),
        user_id=123,
        player_color="white",
        evidence_moves=_final_upload_moves(with_provenance=with_provenance),
        move_count=len(RUY_SANS) + 1,
        dialect_name=db_session.bind.dialect.name,
        run_opportunity=True,
        is_final=True,
    )


def _published_terminal_session(db_session):
    """Seed the captured shape: an accuracy-failed drill with a warm-but-unproven
    batch behind it (the converging cache the poll falls back to) and one freshly
    published terminal scoped delta."""
    if db_session.get(User, 123) is None:
        db_session.add(User(id=123, username=None, is_anonymous=True))
    session = _make_session(
        db_session,
        baseline=_baseline_json({}),
        session_mode="drill",
        drill_state="failed",
        drill_terminal_reason="accuracy",
    )
    _insert_scoped_evidence(db_session, session.id)
    batch_id = _make_batch(db_session, fresh=False)
    _add_score_row(db_session, batch_id=batch_id, opening_key=KP_KEY, opening_score=40.0)
    db_session.commit()
    return session


# ---------------------------------------------------------------------------
# Post-publication invalidation (the successful-publication capture)
# ---------------------------------------------------------------------------


def test_deferred_final_upload_invalidates_terminal_publication_and_recovers(
    db_session,
):
    """The captured sequence, reproduced without any deployment crossing it.

    Order is the whole bug: publish, THEN the deferred analysis-cache write. The
    reader's rejection is correct — that row is shared evidence at a scored position.
    What used to follow was a ten-second wait on the whole-graph worker; what follows
    now is one bounded request for a replacement run.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    cache_rows_before = db_session.query(AnalysisCache).count()

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert (
            publish_scoped_opening_score_deltas(db_session, 123, "white", (request,))
            == 1
        )
        publication = _current_scoped_delta(session.id)
        assert publication is not None

        # Before the deferred work lands the poll is already fresh off the scoped
        # publication — the sub-second outcome the lane is built to deliver.
        assert read_opening_score_delta(db_session, session)[1] is True

        _run_deferred_final_upload(db_session, session.id, with_provenance=True)
        db_session.commit()

        with patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            return_value=DeltaLaneEnqueueOutcome.ENQUEUED,
        ) as reenqueue:
            outcomes = [
                read_opening_score_delta(db_session, session) for _ in range(4)
            ]

    # The deferred run wrote shared evidence and moved the global epoch.
    assert db_session.query(AnalysisCache).count() > cache_rows_before
    assert current_cache_epoch(db_session) != publication.cache_epoch

    # Branch identification. The deferred worker never bumps the per-user counter
    # (`/moves` already did that synchronously, BEFORE this publication was stamped),
    # so the publication clears the evidence_seq guard and dies on the epoch re-arm:
    # the new row is inside its own shared scope, so the digest no longer matches.
    assert current_evidence_seq(db_session, 123, "white") == publication.evidence_seq
    assert (
        shared_scope_digest(
            db_session,
            publication.shared_raw_fens,
            publication.shared_norm_fens,
        )
        != publication.scoped_shared_digest
    )

    # Each poll still serves the stale batch — the reader never fabricates freshness
    # it cannot prove — but the first one now asks for a replacement, and the cooldown
    # keeps the remaining three from piling more runs onto the lane.
    assert [is_fresh for _items, is_fresh in outcomes] == [False, False, False, False]
    assert all(items for items, _is_fresh in outcomes)
    assert reenqueue.call_count == 1
    assert reenqueue.call_args.args == (123, "white", session.id)


def test_deferred_final_upload_without_provenance_leaves_the_poll_fresh(db_session):
    """Control for the same deferred run: a refused cache write keeps the poll fresh.

    The retired ``browser-game-v1`` profile stores nothing, so the epoch never moves
    and the publication survives. This is the discriminator between the captured
    zero-written-row runs (fast, fresh) and the one-written-row runs (12 s stale) —
    the *cache write*, not the deferred run itself, is what invalidates.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    cache_rows_before = db_session.query(AnalysisCache).count()

    with _patched_delta_registry(graph, roots):
        request = reserve_scoped_delta_generation(session.id)
        assert (
            publish_scoped_opening_score_deltas(db_session, 123, "white", (request,))
            == 1
        )
        publication = _current_scoped_delta(session.id)
        assert publication is not None

        _run_deferred_final_upload(db_session, session.id, with_provenance=False)
        db_session.commit()

        items, is_fresh = read_opening_score_delta(db_session, session)

    assert db_session.query(AnalysisCache).count() == cache_rows_before
    assert current_cache_epoch(db_session) == publication.cache_epoch
    assert is_fresh is True
    assert items


def test_terminal_scoped_miss_reenqueues_like_active_boundary(db_session):
    """Side-by-side proof that both reader branches now recover.

    Same reader, same kind of scoped miss — a proven request with no publication behind
    it. Each branch asks the lane for exactly one replacement.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()

    terminal = _published_terminal_session(db_session)
    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            return_value=False,
        ),
        patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            return_value=DeltaLaneEnqueueOutcome.ENQUEUED,
        ) as terminal_enqueue,
    ):
        # No publication at all — the plainest possible scoped miss.
        assert _current_scoped_delta(terminal.id) is None
        items, is_fresh = read_opening_score_delta(db_session, terminal)
    assert is_fresh is False
    assert items  # stale fallback still served while the replacement runs
    assert terminal_enqueue.call_count == 1

    boundary = _make_session(db_session, baseline=_baseline_json({}), status="active")
    _insert_scoped_evidence(db_session, boundary.id)
    boundary = db_session.get(GameSession, boundary.id, populate_existing=True)
    boundary.opening_phase_protocol_version = 1
    boundary.opening_middle_candidate_ply = 7
    boundary.opening_middle_ready_at = utcnow()
    boundary.opening_middle_ply = 7
    db_session.commit()

    division = Division(middle=7, end=None, plies=8)
    with (
        _patched_delta_registry(graph, roots),
        patch("app.opening_score_delta.divide", return_value=division),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            return_value=False,
        ),
        patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            return_value=DeltaLaneEnqueueOutcome.ENQUEUED,
        ) as boundary_enqueue,
    ):
        snapshot = active_prefix_snapshot(db_session, boundary)
        assert snapshot is not None
        # Same miss shape: a token the reader can prove but no publication behind it.
        assert _current_scoped_delta(boundary.id) is None
        assert read_opening_score_delta(
            db_session,
            boundary,
            reconciliation_token=snapshot.reconciliation_token,
        ) == ([], False)
    assert boundary_enqueue.call_count == 1


# ---------------------------------------------------------------------------
# Safe discard with no retry (the counter_drift capture)
# ---------------------------------------------------------------------------


def test_counter_drift_discard_schedules_no_lane_retry_but_the_poll_recovers(
    db_session,
):
    """The second captured shape, and why the reader-side fix is enough for it.

    ``counter_drift`` means the publisher's reads may span incompatible snapshots, so
    discarding is right. It returns normally though, and the lane's requeue lives in an
    ``except`` block, so ``retry_scheduled`` stays 0 and the lane itself never reruns.
    Nothing here changes that — the poll finds no publication, which is the same scoped
    miss as above, and recovers through the reader instead.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    reports: list[dict[str, object]] = []

    def _publish(db, user_id, player_color, requests, *, on_complete=None):
        def _capture(report):
            reports.append(report)
            if on_complete is not None:
                on_complete(report)

        return publish_scoped_opening_score_deltas(
            db, user_id, player_color, requests, on_complete=_capture
        )

    # The lane owns and CLOSES its session, exactly as in production, so it gets
    # its own rather than the test's.
    lane = OpeningScoreDeltaLane(
        session_factory=TestingSessionLocal,
        publish=_publish,
        auto_start=False,
    )

    with _patched_delta_registry(graph, roots):
        with patch(
            # The publisher samples the counter before its evidence reads and again
            # after the digest stage; a write landing in between is the captured race.
            # Scoped to the lane run so the reader's own counter reads stay real.
            "app.opening_score_delta.current_evidence_seq",
            side_effect=[4, 5],
        ):
            lane.enqueue(123, "white", session.id)
            lane.run_due()

        with (
            patch(
                "app.opening_score_delta_lane.is_scoped_delta_scheduled",
                return_value=False,
            ),
            patch(
                "app.opening_score_delta_lane.enqueue_scoped_delta",
                return_value=DeltaLaneEnqueueOutcome.ENQUEUED,
            ) as reenqueue,
        ):
            items, is_fresh = read_opening_score_delta(db_session, session)

    assert reports[0]["outcome"] == "counter_drift"
    assert reports[0]["published_count"] == 0
    assert _current_scoped_delta(session.id) is None
    # Nothing is pending and nothing is scheduled: the discard is terminal.
    assert lane.is_scheduled(123, "white") is False

    assert is_fresh is False
    assert items  # stale fallback again, but a replacement is now on the lane
    assert reenqueue.call_count == 1


# ---------------------------------------------------------------------------
# Recovery bounding and convergence
# ---------------------------------------------------------------------------


def test_recovery_run_republishes_and_the_next_poll_is_fresh(db_session):
    """The race coverage: stall to convergence, without the whole-graph worker.

    The deferred write invalidates the publication, the poll asks for a replacement,
    and the replacement — stamped with the post-write epoch and digest — makes the very
    next poll fresh. This is the whole point of the fix, so it is asserted end to end
    over the real publisher rather than inferred from the enqueue call.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    recovered: list[tuple] = []

    lane = OpeningScoreDeltaLane(
        session_factory=TestingSessionLocal,
        auto_start=False,
    )

    with _patched_delta_registry(graph, roots):
        first = reserve_scoped_delta_generation(session.id)
        assert (
            publish_scoped_opening_score_deltas(db_session, 123, "white", (first,)) == 1
        )
        stale_publication = _current_scoped_delta(session.id)
        assert stale_publication is not None

        _run_deferred_final_upload(db_session, session.id, with_provenance=True)
        db_session.commit()

        with patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            side_effect=lambda *args, **kwargs: (
                recovered.append(args)
                or lane.enqueue(*args, **kwargs)
            ),
        ):
            assert read_opening_score_delta(db_session, session)[1] is False

        # The recovery request the poll placed, run as the lane worker would.
        assert len(recovered) == 1
        lane.run_due()

        items, is_fresh = read_opening_score_delta(db_session, session)

    republished = _current_scoped_delta(session.id)
    assert republished is not None
    assert republished.generation != stale_publication.generation
    assert republished.cache_epoch == current_cache_epoch(db_session)
    assert is_fresh is True
    assert items


def test_recovery_reenqueue_is_cooldown_throttled(db_session):
    """Repeated polls must not pile lane runs up behind one stalled session."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    now = [100.0]

    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            return_value=False,
        ),
        patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            return_value=DeltaLaneEnqueueOutcome.ENQUEUED,
        ) as enqueue,
        patch("app.opening_score_delta.time.monotonic", side_effect=lambda: now[0]),
    ):
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 1

        # Inside the cooldown: the ordinary ~1.5-2 s poll cadence adds nothing.
        for tick in (101.5, 103.0, 105.0, 109.9):
            now[0] = tick
            assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 1

        # Past it, a still-stalled session may try once more.
        now[0] = 110.1
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 2


def test_recovery_is_suppressed_while_a_run_is_already_scheduled(db_session):
    """A pending/in-flight run is the answer the next poll will read; asking again
    would only supersede its generation and discard work in flight."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            return_value=True,
        ),
        patch("app.opening_score_delta_lane.enqueue_scoped_delta") as enqueue,
    ):
        assert read_opening_score_delta(db_session, session)[1] is False

    enqueue.assert_not_called()


def test_recovery_is_suppressed_for_an_evidence_ineligible_session(db_session):
    """The publisher drops an ineligible terminal candidate, so re-enqueueing one
    would burn a lane run per cooldown and never produce a publication."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    session = db_session.get(GameSession, session.id, populate_existing=True)
    # An off-route drill stop: terminal for the client, but not digest-visible.
    session.status = "abandoned"
    session.drill_terminal_reason = "off_route"
    db_session.commit()
    assert session_is_evidence_eligible(session) is False

    with (
        _patched_delta_registry(graph, roots),
        patch("app.opening_score_delta_lane.enqueue_scoped_delta") as enqueue,
    ):
        assert read_opening_score_delta(db_session, session)[1] is False

    enqueue.assert_not_called()


def test_a_fresh_batch_never_triggers_recovery(db_session):
    """Placement guard: recovery lives BELOW the persisted-batch freshness check, so a
    provably fresh batch serves without ever touching the lane."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    batch_id = _make_fresh_batch_for_registry(db_session, graph, roots, generation=2)
    _add_score_row(db_session, batch_id=batch_id, opening_key=KP_KEY, opening_score=55.0)
    db_session.commit()

    with (
        _patched_delta_registry(graph, roots),
        patch("app.opening_score_delta_lane.enqueue_scoped_delta") as enqueue,
    ):
        items, is_fresh = read_opening_score_delta(db_session, session)

    assert is_fresh is True
    assert items
    enqueue.assert_not_called()


def test_recovery_retries_once_the_inputs_move_and_then_caps(db_session):
    """A miss with no publication does not prove the write that caused it has landed.

    The ``counter_drift`` shape has no invalidated publication to reason from, and the
    client's first poll fires about when the deferred worker's quiet window expires, so
    the first attempt can be launched into the write it was racing. A time-only
    cooldown as long as the stall would spend the session's one attempt there and hold
    until the whole-graph worker made it moot. Movement in ``evidence_seq`` or
    ``cache_epoch`` is the proof that another run can reach a different verdict; the
    floor and the cap are what stop churn from turning every poll into a lane run.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    now = [100.0]

    def _bump_epoch() -> None:
        db_session.execute(
            text("UPDATE evidence_epoch SET value = value + 1 WHERE id = 1")
        )
        db_session.commit()

    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            return_value=False,
        ),
        patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            return_value=DeltaLaneEnqueueOutcome.ENQUEUED,
        ) as enqueue,
        patch("app.opening_score_delta.time.monotonic", side_effect=lambda: now[0]),
    ):
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 1

        # Past the floor, but nothing has moved: another run would reach the same
        # verdict, so the plain cooldown holds.
        now[0] = 102.5
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 1

        # The deferred write lands. Now a second attempt can see settled inputs.
        _bump_epoch()
        now[0] = 104.5
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 2

        # Movement inside the floor still waits out one poll cadence.
        _bump_epoch()
        now[0] = 105.0
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 2

        now[0] = 107.0
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 3

        # Capped: continued churn buys nothing more inside this cooldown window.
        _bump_epoch()
        now[0] = 109.5
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 3


def test_recovery_never_supersedes_a_publication_from_an_inflight_run(db_session):
    """A terminal reservation always mints a NEW generation, which makes the current
    publication stale. So the in-flight probe is sampled before the validation read:
    with nothing scheduled beforehand nothing can publish during the read, and with
    something scheduled the poll must not race it. Here the in-flight run lands at the
    moment the poll starts — the publication has to be served, never discarded.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    landed: list[bool] = []

    def _probe_publishes_then_reports_inflight(*_args, **_kwargs) -> bool:
        if not landed:
            request = reserve_scoped_delta_generation(session.id)
            assert (
                publish_scoped_opening_score_deltas(
                    db_session, 123, "white", (request,)
                )
                == 1
            )
            landed.append(True)
        return True

    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            side_effect=_probe_publishes_then_reports_inflight,
        ),
        patch("app.opening_score_delta_lane.enqueue_scoped_delta") as enqueue,
    ):
        items, is_fresh = read_opening_score_delta(db_session, session)

    assert landed == [True]
    assert is_fresh is True
    assert items
    enqueue.assert_not_called()


def test_a_refused_lane_enqueue_does_not_spend_the_recovery_budget(db_session):
    """An overflowed or shut-down lane never ran anything, so the claim is released
    rather than holding this session out for a whole cooldown."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            return_value=False,
        ),
        patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            return_value=DeltaLaneEnqueueOutcome.PENDING_KEY_OVERFLOW,
        ) as enqueue,
    ):
        assert read_opening_score_delta(db_session, session)[1] is False
        assert read_opening_score_delta(db_session, session)[1] is False

    assert enqueue.call_count == 2


def test_a_failing_recovery_still_serves_the_stale_fallback(db_session):
    """Recovery is supplementary. Nothing inside it may cost the poll its fallback —
    which is why the eligibility read sits inside the swallowing boundary too."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta.current_evidence_seq",
            side_effect=RuntimeError("counter read exploded"),
        ),
    ):
        items, is_fresh = read_opening_score_delta(db_session, session)

    assert is_fresh is False
    assert items


@pytest.mark.parametrize("mover", ("cache_epoch", "evidence_seq"))
def test_either_counter_moving_buys_another_attempt(db_session, mover):
    """Both stamped counters are load-bearing, and they move for different reasons.

    The analysis-cache write that produced the captured stall moves only the global
    ``cache_epoch``; a per-user evidence write moves only ``evidence_seq``. Gating on
    one would leave the other's writes invisible to recovery.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    now = [100.0]

    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            return_value=False,
        ),
        patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            return_value=DeltaLaneEnqueueOutcome.ENQUEUED,
        ) as enqueue,
        patch("app.opening_score_delta.time.monotonic", side_effect=lambda: now[0]),
    ):
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 1

        # Past the floor but with nothing moved, the plain cooldown holds.
        now[0] = 102.0
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 1

        if mover == "cache_epoch":
            db_session.execute(
                text("UPDATE evidence_epoch SET value = value + 1 WHERE id = 1")
            )
        else:
            bump_evidence_seq(db_session, 123, "white")
        db_session.commit()

        now[0] = 103.5
        assert read_opening_score_delta(db_session, session)[1] is False
        assert enqueue.call_count == 2


def test_counter_drift_recovery_converges_after_the_deferred_write(db_session):
    """Shape 2, end to end over the real lane and the real deferred worker.

    This is the case the first cut of the fix got wrong. The lane discards
    ``counter_drift`` and publishes nothing, so the first poll's recovery attempt is
    spent before the deferred write has landed and its replacement is invalidated in
    turn. With one attempt per stall that stranded the poll until the whole-graph
    worker; with movement buying another, the poll converges on its own.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    lane = OpeningScoreDeltaLane(
        session_factory=TestingSessionLocal,
        auto_start=False,
    )
    recovery_enqueues: list[tuple] = []
    now = [100.0]

    with _patched_delta_registry(graph, roots):
        with patch(
            # Scoped to the lane run so the reader's own counter reads stay real.
            "app.opening_score_delta.current_evidence_seq",
            side_effect=[4, 5],
        ):
            lane.enqueue(123, "white", session.id)
            lane.run_due()
        # The captured discard: a correct, silent, un-retried zero-publication run.
        assert _current_scoped_delta(session.id) is None

        with (
            patch(
                "app.opening_score_delta_lane.enqueue_scoped_delta",
                side_effect=lambda *args, **kwargs: (
                    recovery_enqueues.append(args) or lane.enqueue(*args, **kwargs)
                ),
            ),
            patch(
                "app.opening_score_delta.time.monotonic", side_effect=lambda: now[0]
            ),
        ):
            # First poll recovers, and its replacement publishes against inputs that
            # have not settled yet — nothing here proves the write has landed.
            assert read_opening_score_delta(db_session, session)[1] is False
            assert len(recovery_enqueues) == 1
            lane.run_due()
            assert _current_scoped_delta(session.id) is not None

            # The deferred final upload lands AFTER that replacement, killing it.
            _run_deferred_final_upload(db_session, session.id, with_provenance=True)
            db_session.commit()

            fresh: list[bool] = []
            for tick in (101.5, 103.0, 104.5):
                now[0] = tick
                fresh.append(read_opening_score_delta(db_session, session)[1])
                lane.run_due()

    # One more attempt, bought by the movement the write caused, and the poll is fresh
    # on the next cycle instead of waiting out the whole-graph worker.
    assert fresh == [False, True, True]
    assert len(recovery_enqueues) == 2


def test_a_request_arriving_during_the_read_also_suppresses_recovery(db_session):
    """The in-flight window is two-sided.

    Sampling only before the read covers a run that FINISHES during it. A terminal POST
    for this same session can equally enqueue DURING it, and recovery would then mint a
    newer generation and supersede that request. Here the probe reports nothing
    scheduled before the read and a request present after it.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with (
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            side_effect=[False, True],
        ) as probe,
        patch("app.opening_score_delta_lane.enqueue_scoped_delta") as enqueue,
    ):
        items, is_fresh = read_opening_score_delta(db_session, session)

    assert probe.call_count == 2
    assert is_fresh is False
    assert items
    enqueue.assert_not_called()


def test_recovery_log_reports_the_attempts_actually_spent(db_session, caplog):
    """The log line exists to measure this fix in the next pilot, so a suppressed poll
    has to report the attempts already spent, not the one it would have made."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    now = [100.0]

    with (
        caplog.at_level(logging.INFO, logger="app.opening_score_delta"),
        _patched_delta_registry(graph, roots),
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            return_value=False,
        ),
        patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            return_value=DeltaLaneEnqueueOutcome.ENQUEUED,
        ),
        patch("app.opening_score_delta.time.monotonic", side_effect=lambda: now[0]),
    ):
        read_opening_score_delta(db_session, session)
        db_session.execute(
            text("UPDATE evidence_epoch SET value = value + 1 WHERE id = 1")
        )
        db_session.commit()
        now[0] = 102.0
        read_opening_score_delta(db_session, session)

        caplog.clear()
        now[0] = 104.0  # past the floor, nothing moved since the second attempt
        read_opening_score_delta(db_session, session)

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("terminal_delta_recovery ")
    ]
    assert len(lines) == 1
    assert "outcome=cooldown" in lines[0]
    assert "attempt=2" in lines[0]
