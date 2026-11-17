"""One closed-enum reason per scoped-delta reader branch (g-delta-read-reason).

``read_opening_score_delta`` used to log nothing at all, and every way a scoped
publication can be refused — missing, bound to the other lane, outrun by the
evidence counter, killed by a shared-scope write — degraded to the same stale
fallback. Diagnosing g-delta-stale-publish therefore cost a source read plus a
purpose-built reproduction to learn which guard had fired in production, which a
one-token log field would have answered directly.

So each read now emits one ``opening_score_delta_read`` line naming the precedence
branch that served (``source``), the lane whose publication was examined
(``scoped``), and the guard that refused it (``reason``). The tests below pin one
reason per branch through :func:`_assert_reason`, which records what it asserted, and
:func:`_reason_coverage_ledger` fails the module if a branch was never driven.

OBSERVABILITY ONLY. Nothing here asserts a changed verdict: the reader serves exactly
what it served before, and the assertions on returned items/freshness alongside every
reason are what pins that.
"""

import logging
import uuid
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.game_phase import Division
from app.models import GameSession
from app.opening_evidence import session_is_evidence_eligible
from app.opening_score_delta import (
    SCOPED_DELTA_SOURCE_OPENING_BOUNDARY,
    SCOPED_DELTA_SOURCE_TERMINAL,
    OpeningDeltaReadSource,
    ScopedDeltaRejection,
    _current_scoped_delta,
    _publish_scoped_delta,
    _scoped_delta_publications,
    _validated_scoped_score_map,
    active_prefix_snapshot,
    publish_scoped_opening_score_deltas,
    read_opening_score_delta,
    reserve_scoped_delta_generation,
)
from app.opening_score_delta_lane import (
    DeltaLaneEnqueueOutcome,
    OpeningScoreDeltaLane,
)
from conftest import TestingSessionLocal
from app.session_contracts import utcnow

# The synthetic Ruy registry and the captured terminal shape are shared with the
# suites that already reproduce this flow; re-seeding them here would let the
# reproductions drift apart.
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
from test_opening_score_delta_terminal_recovery import (
    _published_terminal_session,
    _run_deferred_final_upload,
)

_LOG_PREFIX = "opening_score_delta_read "

# Filled by _assert_reason, drained by _reason_coverage_ledger.
_REASONS_PINNED: set[ScopedDeltaRejection] = set()


def _assert_reason(observed, expected: ScopedDeltaRejection):
    """Assert one branch label, and record that a test drove that branch.

    Recording happens HERE, at the assertion, so coverage means a test actually
    reached the guard. The hand-kept list this replaced passed the moment a new
    member's name was added to it, test or no test.
    """
    value = (
        observed.value if isinstance(observed, ScopedDeltaRejection) else observed
    )
    assert value == expected.value
    _REASONS_PINNED.add(expected)


@pytest.fixture(scope="module", autouse=True)
def _reason_coverage_ledger(request):
    """Fail the module when a ``ScopedDeltaRejection`` member no test ever pinned.

    A new guard that logs a reason nothing exercises is the exact gap this bead
    closed. Enforced only when the whole module ran, since otherwise the unpinned
    branches are the selection's doing. Two checks, because neither covers both ways
    of narrowing a run:

    * ``-k`` and ``--deselect`` can drop one parametrize case and keep every function
      name, which a name comparison cannot see, so either option skips enforcement.
      ``-m`` does not: the pre-push gate always passes ``-m "not release_seal"``, and
      no test here carries a marker it could deselect.
    * Node ids on the command line narrow collection itself, so nothing is reported
      as deselected. Comparing the selected function names catches that.
    """
    yield
    if request.config.getoption("keyword") or request.config.getoption("deselect"):
        return
    prefix = f"{request.node.nodeid}::"
    selected = {
        item.originalname or item.name
        for item in request.session.items
        if item.nodeid.startswith(prefix)
    }
    if selected != {name for name in vars(request.module) if name.startswith("test_")}:
        return
    unpinned = sorted(
        member.value
        for member in ScopedDeltaRejection
        if member not in _REASONS_PINNED
    )
    assert not unpinned, f"no test drives these rejection branches: {unpinned}"


def _quiet_lane(lane=None):
    """Keep a scoped miss's best-effort re-enqueue off the running lane.

    A terminal miss asks ``enqueue_terminal_delta_recovery`` for a replacement run and
    a boundary miss re-enqueues its own token; neither is under test here. Pass an
    unstarted ``lane`` to let that request land for real: the lane reserves the new
    generation inside ``enqueue``, which is what the run it schedules will publish
    under, and nothing executes until ``run_due``.
    """
    return (
        patch(
            "app.opening_score_delta_lane.is_scoped_delta_scheduled",
            return_value=False,
        ),
        patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            return_value=DeltaLaneEnqueueOutcome.ENQUEUED,
        )
        if lane is None
        else patch(
            "app.opening_score_delta_lane.enqueue_scoped_delta",
            side_effect=lambda *args, **kwargs: lane.enqueue(*args, **kwargs),
        ),
    )


def _read(caplog, db_session, session, *, lane=None, **kwargs):
    """Read once and return ``(result, logged fields, raw line)``.

    Asserts exactly one line per read: the poll fires every ~1.5 s per session, so a
    duplicated or missing line would misreport the next capture's attempt counts.
    """
    caplog.clear()
    scheduled, enqueue = _quiet_lane(lane)
    with caplog.at_level(logging.INFO, logger="app.opening_score_delta"):
        with scheduled, enqueue:
            result = read_opening_score_delta(db_session, session, **kwargs)
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(_LOG_PREFIX)
    ]
    assert len(lines) == 1
    fields = dict(
        part.split("=", 1) for part in lines[0].split(" ") if "=" in part
    )
    return result, fields, lines[0]


def _publish_terminal(db_session, session):
    """One freshly published terminal scoped delta, through the real publisher."""
    request = reserve_scoped_delta_generation(session.id)
    assert publish_scoped_opening_score_deltas(
        db_session, 123, "white", (request,)
    ) == 1
    publication = _current_scoped_delta(session.id)
    assert publication is not None
    return publication


def _mutate(session_id, **changes):
    """Rewrite the live publication in place, keeping its generation.

    Each guard is reached by moving exactly one stamped field away from the state it
    was proven against — the same divergence production produces by racing, without
    having to reproduce the race.
    """
    publication = _current_scoped_delta(session_id)
    assert publication is not None
    mutated = replace(publication, **changes)
    assert _publish_scoped_delta(mutated)
    return mutated


def _boundary_session(db_session):
    """An active session with a proven opening prefix, ready to publish against."""
    session = _make_session(db_session, baseline=_baseline_json({}), status="active")
    _insert_scoped_evidence(db_session, session.id)
    session = db_session.get(GameSession, session.id, populate_existing=True)
    session.opening_phase_protocol_version = 1
    session.opening_middle_candidate_ply = 7
    session.opening_middle_ready_at = utcnow()
    session.opening_middle_ply = 7
    db_session.commit()
    return session


# ---------------------------------------------------------------------------
# Accepted reads: which lane got there first
# ---------------------------------------------------------------------------


def test_a_terminal_scoped_hit_names_the_terminal_lane(db_session, caplog):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        _publish_terminal(db_session, session)
        (items, is_fresh), fields, _line = _read(caplog, db_session, session)

    assert (bool(items), is_fresh) == (True, True)
    assert fields["source"] == OpeningDeltaReadSource.SCOPED_TERMINAL.value
    assert fields["scoped"] == SCOPED_DELTA_SOURCE_TERMINAL
    assert fields["reason"] == "none"


def test_a_boundary_scoped_hit_names_the_boundary_lane(db_session, caplog):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _boundary_session(db_session)
    division = Division(middle=7, end=None, plies=8)

    with (
        _patched_delta_registry(graph, roots),
        patch("app.opening_score_delta.divide", return_value=division),
    ):
        snapshot = active_prefix_snapshot(db_session, session)
        assert snapshot is not None
        request = reserve_scoped_delta_generation(
            session.id,
            source=SCOPED_DELTA_SOURCE_OPENING_BOUNDARY,
            reconciliation_token=snapshot.reconciliation_token,
        )
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        (items, is_fresh), fields, _line = _read(
            caplog,
            db_session,
            session,
            reconciliation_token=snapshot.reconciliation_token,
        )

    assert (bool(items), is_fresh) == (True, True)
    assert fields["source"] == OpeningDeltaReadSource.SCOPED_BOUNDARY.value
    assert fields["scoped"] == SCOPED_DELTA_SOURCE_OPENING_BOUNDARY
    assert fields["reason"] == "none"


def test_a_fresh_batch_consults_no_publication(db_session, caplog):
    """Precedence, not a verdict: the fresh batch outranks the scoped lane, so no
    publication is examined and there is no reason to report."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    batch_id = _make_fresh_batch_for_registry(db_session, graph, roots, generation=2)
    _add_score_row(db_session, batch_id=batch_id, opening_key=KP_KEY, opening_score=55.0)
    db_session.commit()

    with _patched_delta_registry(graph, roots):
        _publish_terminal(db_session, session)
        (items, is_fresh), fields, _line = _read(caplog, db_session, session)

    assert (bool(items), is_fresh) == (True, True)
    assert fields["source"] == OpeningDeltaReadSource.FRESH_BATCH.value
    assert fields["scoped"] == "none"
    assert fields["reason"] == "none"


# ---------------------------------------------------------------------------
# One reason per rejecting guard
# ---------------------------------------------------------------------------

# Each case moves exactly one stamped field off the state it was proven against.
# ``None`` publishes nothing at all, which is ABSENT in the strict sense — a stored
# publication the counter has passed is SUPERSEDED when it is the reading lane's and
# OTHER_LANE_LEFTOVER when it is not, both below.
_MUTATION_CASES = (
    (ScopedDeltaRejection.ABSENT, None),
    (ScopedDeltaRejection.SOURCE_MISMATCH, {"source": SCOPED_DELTA_SOURCE_OPENING_BOUNDARY}),
    (ScopedDeltaRejection.TOKEN_MISMATCH, {"reconciliation_token": "a-token"}),
    (ScopedDeltaRejection.SESSION_IDENTITY, {"drill_state": "passed"}),
    (ScopedDeltaRejection.PLAYED_CHAIN, {"played_root_keys": ("not-a-played-key",)}),
    (ScopedDeltaRejection.REGISTRY_DRIFT, {"registry_fingerprint": "other-registry"}),
)

@pytest.mark.parametrize(
    ("reason", "changes"),
    _MUTATION_CASES,
    ids=[reason.value for reason, _changes in _MUTATION_CASES],
)
def test_each_binding_guard_names_itself(db_session, caplog, reason, changes):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        if changes is not None:
            _publish_terminal(db_session, session)
            _mutate(session.id, **changes)
        (items, is_fresh), fields, _line = _read(caplog, db_session, session)

    # Unchanged behaviour: the warm batch still serves, still unverified.
    assert (bool(items), is_fresh) == (True, False)
    assert fields["source"] == OpeningDeltaReadSource.STALE_BATCH.value
    assert fields["scoped"] == SCOPED_DELTA_SOURCE_TERMINAL
    _assert_reason(fields["reason"], reason)


def test_a_moved_evidence_counter_names_itself(db_session, caplog):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        publication = _publish_terminal(db_session, session)
        _mutate(session.id, evidence_seq=publication.evidence_seq + 1)
        _result, fields, _line = _read(caplog, db_session, session)

    _assert_reason(fields["reason"], ScopedDeltaRejection.EVIDENCE_SEQ)


def test_an_unreadable_epoch_names_itself(db_session, caplog):
    """The epoch readers fail closed on: no epoch, no proof, no scoped result."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        _publish_terminal(db_session, session)
        with patch(
            "app.opening_score_delta.current_cache_epoch", return_value=None
        ):
            _result, fields, _line = _read(caplog, db_session, session)

    _assert_reason(fields["reason"], ScopedDeltaRejection.EPOCH_MISSING)


def test_the_deferred_upload_that_stalled_g_6qwoj_names_the_shared_digest(
    db_session, caplog
):
    """The captured stall, now self-describing.

    The deferred ``/moves`` side effects land AFTER the publication, write an
    analysis-cache row at a position this session played, and move the global epoch.
    The row is inside the publication's own shared scope, so the epoch re-arm fails
    its digest guard — correctly. That is the branch g-delta-stale-publish had to
    reproduce locally to identify; a production capture now reads it off the line.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        _publish_terminal(db_session, session)
        assert read_opening_score_delta(db_session, session)[1] is True

        _run_deferred_final_upload(db_session, session.id, with_provenance=True)
        db_session.commit()
        (items, is_fresh), fields, _line = _read(caplog, db_session, session)

    assert (bool(items), is_fresh) == (True, False)
    assert fields["source"] == OpeningDeltaReadSource.STALE_BATCH.value
    _assert_reason(fields["reason"], ScopedDeltaRejection.SHARED_DIGEST)


def test_the_poll_after_a_recovery_enqueue_names_a_superseded_generation(
    db_session, caplog
):
    """Two polls of one stall — which is what a capture actually contains.

    The first miss asks for a replacement run, and a terminal reservation always
    takes a new generation, so every poll until that run publishes reads a
    publication the request counter has already passed. At ~1.5 s per poll a stall is
    mostly made of these lines, and under one ``absent`` label they said neither which
    guard started it nor that there had been a publication at all. Whether a
    replacement is pending is a separate question that ``terminal_delta_recovery``
    answers, for this branch and for ``absent`` alike.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)
    # Never started and never run: the replacement is queued, not published, which
    # is the state every poll in the gap observes.
    lane = OpeningScoreDeltaLane(
        session_factory=TestingSessionLocal, auto_start=False
    )

    with _patched_delta_registry(graph, roots):
        _publish_terminal(db_session, session)
        _run_deferred_final_upload(db_session, session.id, with_provenance=True)
        db_session.commit()
        _first, first_fields, _first_line = _read(
            caplog, db_session, session, lane=lane
        )
        (items, is_fresh), fields, _line = _read(caplog, db_session, session)

    _assert_reason(first_fields["reason"], ScopedDeltaRejection.SHARED_DIGEST)
    # Still stored — only the request counter moved past it. That is the whole
    # difference between the two labels, so assert it rather than infer it.
    assert str(session.id) in _scoped_delta_publications
    assert _current_scoped_delta(session.id) is None

    # Unchanged behaviour: the warm batch keeps serving while recovery is pending.
    assert (bool(items), is_fresh) == (True, False)
    assert fields["source"] == OpeningDeltaReadSource.STALE_BATCH.value
    _assert_reason(fields["reason"], ScopedDeltaRejection.SUPERSEDED)


def test_a_boundary_leftover_under_a_terminal_request_is_not_superseded(
    db_session, caplog
):
    """The production sequence whenever boundary publication is on.

    The session publishes at the opening boundary, ends, and the terminal request
    mints a new generation. Both lanes share one slot and an overtaken publication is
    never removed, so until the terminal lane publishes, the slot still holds the
    boundary one. Labelled ``superseded``, every terminal poll in that gap claimed a
    terminal publication that never existed, including when the terminal run was
    discarded and stored nothing.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _boundary_session(db_session)
    batch_id = _make_batch(db_session, fresh=False)
    _add_score_row(db_session, batch_id=batch_id, opening_key=KP_KEY, opening_score=40.0)
    db_session.commit()
    division = Division(middle=7, end=None, plies=8)

    with (
        _patched_delta_registry(graph, roots),
        patch("app.opening_score_delta.divide", return_value=division),
    ):
        snapshot = active_prefix_snapshot(db_session, session)
        assert snapshot is not None
        boundary_request = reserve_scoped_delta_generation(
            session.id,
            source=SCOPED_DELTA_SOURCE_OPENING_BOUNDARY,
            reconciliation_token=snapshot.reconciliation_token,
        )
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (boundary_request,)
        ) == 1

        session = db_session.get(GameSession, session.id, populate_existing=True)
        session.status = "ended"
        session.result = "checkmate_win"
        db_session.commit()
        # What the terminal POST reserves before it enqueues the terminal lane run.
        terminal_request = reserve_scoped_delta_generation(session.id)
        assert terminal_request.generation > boundary_request.generation

        (items, is_fresh), fields, _line = _read(caplog, db_session, session)

    # The boundary publication is what is stored; no terminal one ever was.
    leftover = _scoped_delta_publications[str(session.id)]
    assert leftover.source == SCOPED_DELTA_SOURCE_OPENING_BOUNDARY
    assert leftover.generation == boundary_request.generation
    assert _current_scoped_delta(session.id) is None

    # Unchanged behaviour: the warm batch serves, unverified, as for any terminal miss.
    assert (bool(items), is_fresh) == (True, False)
    assert fields["source"] == OpeningDeltaReadSource.STALE_BATCH.value
    assert fields["scoped"] == SCOPED_DELTA_SOURCE_TERMINAL
    _assert_reason(fields["reason"], ScopedDeltaRejection.OTHER_LANE_LEFTOVER)


def test_a_lost_epoch_rearm_names_itself(db_session, caplog):
    """The epoch moved but the shared scope did not, so the publication is still
    provable — and then a concurrent publication takes the slot before the re-arm."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        publication = _publish_terminal(db_session, session)
        _mutate(session.id, cache_epoch=publication.cache_epoch + 1)
        with patch(
            "app.opening_score_delta._rearm_scoped_delta_epoch", return_value=None
        ):
            _result, fields, _line = _read(caplog, db_session, session)

    _assert_reason(fields["reason"], ScopedDeltaRejection.REARM_LOST)


def test_a_superseded_generation_names_the_race(db_session, caplog):
    """The generation re-check that closes the window after the DB proofs: the
    publication read at the top is gone by the time the map is handed back."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        _publish_terminal(db_session, session)
        with patch(
            # The only call left on this name: the validator takes the publication
            # and its absence label from _current_scoped_delta_with_reason, then
            # re-reads here to close the window after the DB proofs.
            "app.opening_score_delta._current_scoped_delta",
            return_value=None,
        ):
            _result, fields, _line = _read(caplog, db_session, session)

    _assert_reason(fields["reason"], ScopedDeltaRejection.GENERATION_RACE)


def test_an_evidence_ineligible_session_names_itself(db_session, caplog):
    """A defensive guard, pinned as one: the publisher applies the same eligibility
    test to a terminal candidate, so only a publication stamped from an already
    ineligible session reaches it — which is why the stamped identity has to be moved
    with the session, or the identity guard fires first."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        _publish_terminal(db_session, session)
        session = db_session.get(GameSession, session.id, populate_existing=True)
        # An off-route drill stop: terminal for the client, but not digest-visible.
        session.status = "abandoned"
        session.drill_terminal_reason = "off_route"
        db_session.commit()
        assert session_is_evidence_eligible(session) is False
        _mutate(session.id, status="abandoned", drill_terminal_reason="off_route")
        _result, fields, _line = _read(caplog, db_session, session)

    _assert_reason(fields["reason"], ScopedDeltaRejection.INELIGIBLE)


def test_a_moved_boundary_prefix_names_itself(db_session, caplog):
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _boundary_session(db_session)
    division = Division(middle=7, end=None, plies=8)

    with (
        _patched_delta_registry(graph, roots),
        patch("app.opening_score_delta.divide", return_value=division),
    ):
        snapshot = active_prefix_snapshot(db_session, session)
        assert snapshot is not None
        request = reserve_scoped_delta_generation(
            session.id,
            source=SCOPED_DELTA_SOURCE_OPENING_BOUNDARY,
            reconciliation_token=snapshot.reconciliation_token,
        )
        assert publish_scoped_opening_score_deltas(
            db_session, 123, "white", (request,)
        ) == 1
        _mutate(session.id, line_revision=(session.move_line_revision or 0) + 1)
        (items, is_fresh), fields, _line = _read(
            caplog,
            db_session,
            session,
            reconciliation_token=snapshot.reconciliation_token,
        )

    # Unchanged behaviour: a boundary miss shows nothing rather than a stale batch.
    assert (items, is_fresh) == ([], False)
    assert fields["source"] == OpeningDeltaReadSource.EMPTY.value
    assert fields["scoped"] == SCOPED_DELTA_SOURCE_OPENING_BOUNDARY
    _assert_reason(fields["reason"], ScopedDeltaRejection.BOUNDARY_PREFIX)


@pytest.mark.parametrize(
    ("reason", "expected_source", "snapshot_patch"),
    [
        (ScopedDeltaRejection.BOUNDARY_SNAPSHOT, SCOPED_DELTA_SOURCE_OPENING_BOUNDARY, True),
        (ScopedDeltaRejection.SOURCE_UNKNOWN, "a-retired-lane", False),
    ],
    ids=[
        ScopedDeltaRejection.BOUNDARY_SNAPSHOT.value,
        ScopedDeltaRejection.SOURCE_UNKNOWN.value,
    ],
)
def test_the_defensive_guards_name_themselves(
    db_session, reason, expected_source, snapshot_patch
):
    """Two guards the reader cannot reach, asserted at the validator instead.

    The reader proves an active prefix snapshot before it ever validates a boundary
    publication, and it only ever asks for the two known lanes. Both guards are still
    live code on a path a future caller could take, so each gets its label pinned.
    """
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        publication = _publish_terminal(db_session, session)
        _mutate(session.id, source=expected_source)
        no_snapshot = (
            patch("app.opening_score_delta.active_prefix_snapshot", return_value=None)
            if snapshot_patch
            else nullcontext()
        )
        with no_snapshot:
            scores, rejected = _validated_scoped_score_map(
                db_session,
                session,
                publication.played_root_keys,
                expected_source=expected_source,
            )

    assert scores is None
    _assert_reason(rejected, reason)


# ---------------------------------------------------------------------------
# The non-scoped precedence branches
# ---------------------------------------------------------------------------


def test_a_session_with_no_played_opening_reports_no_chain(db_session, caplog):
    session = _make_session(db_session, baseline=_baseline_json({}))
    db_session.commit()

    with _patched_delta_registry(_ruy_graph(), _ruy_roots()):
        (items, is_fresh), fields, _line = _read(caplog, db_session, session)

    # Nothing will ever appear, so the poll stops: empty AND fresh.
    assert (items, is_fresh) == ([], True)
    assert fields["source"] == OpeningDeltaReadSource.NO_CHAIN.value
    assert fields["scoped"] == "none"


def test_a_cold_session_with_no_batch_reports_empty(db_session, caplog):
    """No fresh batch, no warm batch, no publication: nothing to serve at all."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _make_session(db_session, baseline=_baseline_json({}))
    _insert_scoped_evidence(db_session, session.id)
    db_session.commit()

    with _patched_delta_registry(graph, roots):
        (items, is_fresh), fields, _line = _read(caplog, db_session, session)

    assert (items, is_fresh) == ([], False)
    assert fields["source"] == OpeningDeltaReadSource.EMPTY.value
    _assert_reason(fields["reason"], ScopedDeltaRejection.ABSENT)


def test_a_missing_session_row_reports_itself(db_session, caplog):
    (items, is_fresh), fields, _line = _read(
        caplog,
        db_session,
        # A session id with no row behind it — the authoritative re-read returns None.
        SimpleNamespace(id=uuid.uuid4()),
    )

    assert (items, is_fresh) == ([], False)
    assert fields["source"] == OpeningDeltaReadSource.MISSING_SESSION.value
    assert fields["scoped"] == "none"


def test_a_stale_reconciliation_token_reports_itself(db_session, caplog):
    session = _boundary_session(db_session)
    division = Division(middle=7, end=None, plies=8)

    with (
        _patched_delta_registry(_ruy_graph(), _ruy_roots()),
        patch("app.opening_score_delta.divide", return_value=division),
    ):
        (items, is_fresh), fields, _line = _read(
            caplog,
            db_session,
            session,
            reconciliation_token="not-the-proven-token",
        )

    assert (items, is_fresh) == ([], False)
    assert fields["source"] == OpeningDeltaReadSource.TOKEN_STALE.value
    assert fields["scoped"] == "none"


def test_a_failing_read_still_logs_and_still_never_raises(db_session, caplog):
    """The log line lives in a ``finally``, so the one read a capture most needs to
    see is the one that broke — and adding it must not break the poll's contract."""
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(_ruy_graph(), _ruy_roots()):
        with patch(
            "app.opening_score_delta.played_opening_chain",
            side_effect=RuntimeError("boom"),
        ):
            (items, is_fresh), fields, _line = _read(caplog, db_session, session)

    assert (items, is_fresh) == ([], False)
    assert fields["source"] == OpeningDeltaReadSource.FAILED.value


# ---------------------------------------------------------------------------
# What the line is allowed to carry
# ---------------------------------------------------------------------------


def test_the_line_carries_branch_labels_and_no_proof_material(db_session, caplog):
    """Closed enum, fixed fields. A reason is a guard's name, never the value it
    compared: digests, tokens, FENs, opening keys and scores stay out of the log."""
    graph = _ruy_graph()
    roots = _ruy_roots()
    session = _published_terminal_session(db_session)

    with _patched_delta_registry(graph, roots):
        publication = _publish_terminal(db_session, session)
        _run_deferred_final_upload(db_session, session.id, with_provenance=True)
        db_session.commit()
        _result, fields, line = _read(caplog, db_session, session)

    assert set(fields) == {"source", "scoped", "reason", "session_id", "read_ms"}
    assert fields["source"] in {member.value for member in OpeningDeltaReadSource}
    assert fields["scoped"] in {
        SCOPED_DELTA_SOURCE_TERMINAL,
        SCOPED_DELTA_SOURCE_OPENING_BOUNDARY,
        "none",
    }
    assert fields["reason"] in {member.value for member in ScopedDeltaRejection}

    # Each list is asserted non-empty first: a loop over nothing proves nothing.
    assert publication.shared_raw_fens and publication.shared_norm_fens
    assert publication.played_root_keys and publication.scored_roots
    for secret in (
        publication.scoped_shared_digest,
        publication.registry_fingerprint,
        *publication.shared_raw_fens,
        *publication.shared_norm_fens,
        *publication.played_root_keys,
        *(key for key, _score in publication.scored_roots),
    ):
        assert secret not in line
