"""The drill evidence boundary and the broad opportunity rows it scopes.

A drill walks the player down a SCRIPTED route to an opening root. Those plies are the
server's choice, not the player's, and the steering path already treats them as
non-evidence. The accounting path used to hash every ``session_moves`` row, so route
walking inflated dueness counters for every blunder in its 8-ply forward neighbourhood
and routed arrival at the root was miscredited as a ghost REACH (g-ghost-preach-absorb).

This suite owns that contract: which observations count, in which of the two roles, and
what a session with no boundary at all contributes. The chess is deliberately trivial —
two lone kings walking — because every assertion here is about PLY ARITHMETIC and set
membership, not about move legality.

The shared board (player = white, so the opponent is black):

    ply 0  P0  Ka1/kh8   white to move   route start
    ply 1  P1  Kb1/kh8   black to move   route ply — a pre-boundary opponent position
    ply 2  P2  Kb1/kg8   white to move
    ply 3  R   Kc1/kg8   black to move   THE ROOT — boundary, opponent to move
    ply 4  P4  Kc1/kf8   white to move
    ply 5  P5  Kd1/kf8   black to move

    edges: R -> D   (D is only ever reachable from the root)
           P1 -> X  (X is only ever reachable from the PRE-boundary route ply)

``D`` proves the root still works as a BFS seed; ``X`` proves the pre-boundary prefix no
longer does.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.api.session import _compute_blunder_opportunity_events
from app.evidence_boundary import (
    NORMAL_SESSION_EVIDENCE_START_PLY,
    evidence_start_ply,
    observed_position_plies,
    session_evidence_hashes,
    split_evidence_hashes,
)
from app.fen import fen_hash
from app.models import Blunder, BlunderOpportunityEvent, GameSession, Move, Position, SessionMove

USER_ID = 4242

P0 = "7k/8/8/8/8/8/8/K7 w - - 0 1"
P1 = "7k/8/8/8/8/8/8/1K6 b - - 1 1"
P2 = "6k1/8/8/8/8/8/8/1K6 w - - 2 2"
ROOT = "6k1/8/8/8/8/8/8/2K5 b - - 3 2"
P4 = "5k2/8/8/8/8/8/8/2K5 w - - 4 3"
P5 = "5k2/8/8/8/8/8/8/3K4 b - - 5 3"
P6 = "6k1/8/8/8/8/8/8/3K4 w - - 6 4"
# Same NORMALIZED position as ROOT (fen_hash strips the clocks), reached again at ply 7.
ROOT_AGAIN = "6k1/8/8/8/8/8/8/2K5 b - - 7 4"
# Forward-reachable from ROOT only.
DOWNSTREAM = "7k/8/8/8/8/8/8/2K5 w - - 4 3"
# Forward-reachable from the PRE-boundary route ply P1 only.
PRE_ROUTE_CHILD = "8/7k/8/8/8/8/8/1K6 w - - 2 2"

# (move_number, color, fen_before, fen_after) — the route to the root, then two plies of
# real post-root play. ply_after(move_number, color) dates each fen_after; fen_before is
# one ply earlier.
ROUTE_AND_PLAY = [
    (1, "white", P0, P1),
    (1, "black", P1, P2),
    (2, "white", P2, ROOT),
    (2, "black", ROOT, P4),
    (3, "white", P4, P5),
]
# Two further plies that walk back INTO the root at ply 7.
REVISIT_ROOT = [
    (3, "black", P5, P6),
    (4, "white", P6, ROOT_AGAIN),
]


def _position(db_session, fen: str) -> Position:
    parts = fen.split(" ")
    position = Position(
        user_id=USER_ID,
        fen_hash=fen_hash(fen),
        fen_raw=fen,
        active_color="white" if parts[1] == "w" else "black",
    )
    db_session.add(position)
    db_session.flush()
    return position


def _blunder(db_session, position: Position) -> Blunder:
    blunder = Blunder(
        user_id=USER_ID,
        position_id=position.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(blunder)
    db_session.flush()
    return blunder


def _board(db_session) -> dict[str, Blunder]:
    """Every position on the shared board, the two edges, and the three blunders."""
    positions = {
        fen: _position(db_session, fen)
        for fen in (P0, P1, P2, ROOT, P4, P5, P6, DOWNSTREAM, PRE_ROUTE_CHILD)
    }
    db_session.add_all(
        [
            Move(
                from_position_id=positions[ROOT].id,
                move_san="Kh8",
                to_position_id=positions[DOWNSTREAM].id,
            ),
            Move(
                from_position_id=positions[P1].id,
                move_san="Kh7",
                to_position_id=positions[PRE_ROUTE_CHILD].id,
            ),
        ]
    )
    return {
        "root": _blunder(db_session, positions[ROOT]),
        "downstream": _blunder(db_session, positions[DOWNSTREAM]),
        "pre_route": _blunder(db_session, positions[PRE_ROUTE_CHILD]),
        "start": _blunder(db_session, positions[P0]),
    }


def _drill(
    db_session,
    *,
    drill_state: str = "root_reached",
    root_ply: int | None = 3,
    rated_start_ply: int | None = None,
    moves=ROUTE_AND_PLAY,
) -> GameSession:
    now = datetime.now(timezone.utc)
    game_session = GameSession(
        id=uuid.uuid4(),
        user_id=USER_ID,
        started_at=now,
        status="active",
        engine_elo=1500,
        player_color="white",
        session_mode="drill",
        drill_state=drill_state,
        drill_opening_key="test-opening",
        is_rated=drill_state == "converted",
        drill_root_reached_ply=root_ply,
        rated_start_ply=rated_start_ply,
    )
    if drill_state == "converted":
        game_session.normal_started_at = now
        game_session.converted_at = now
    db_session.add(game_session)
    db_session.flush()
    _add_moves(db_session, game_session, moves)
    return game_session


def _normal(db_session, *, moves=ROUTE_AND_PLAY) -> GameSession:
    game_session = GameSession(
        id=uuid.uuid4(),
        user_id=USER_ID,
        started_at=datetime.now(timezone.utc),
        status="active",
        engine_elo=1500,
        player_color="white",
    )
    db_session.add(game_session)
    db_session.flush()
    _add_moves(db_session, game_session, moves)
    return game_session


def _add_moves(db_session, game_session: GameSession, moves) -> None:
    for move_number, color, fen_before, fen_after in moves:
        db_session.add(
            SessionMove(
                session_id=game_session.id,
                move_number=move_number,
                color=color,
                move_san="K",
                fen_before=fen_before,
                fen_after=fen_after,
                segment="drill" if game_session.session_mode == "drill" else "normal",
            )
        )


def _events(db_session, game_session: GameSession) -> dict[int, tuple[bool, bool]]:
    return {
        row.blunder_id: (bool(row.opportunity), bool(row.reached))
        for row in db_session.query(BlunderOpportunityEvent).filter_by(
            session_id=game_session.id
        )
    }


def _recompute(db_session, game_session: GameSession) -> dict[int, tuple[bool, bool]]:
    _compute_blunder_opportunity_events(
        db_session,
        session_id=game_session.id,
        user_id=USER_ID,
        player_color=game_session.player_color,
    )
    db_session.commit()  # the caller owns the commit
    return _events(db_session, game_session)


# ---------------------------------------------------------------------------
# evidence_start_ply — the boundary contract itself
# ---------------------------------------------------------------------------


def test_normal_session_boundary_admits_the_starting_position():
    # -1, not 0: the reach rule is STRICTLY greater than the boundary, and ply 0 (the
    # starting position, carried only by the first row's fen_before) has always been a
    # reach. A boundary of 0 would silently drop it.
    session = GameSession(session_mode="normal")
    assert evidence_start_ply(session) == NORMAL_SESSION_EVIDENCE_START_PLY == -1


@pytest.mark.parametrize(
    "root_ply,rated_start_ply,expected",
    [
        (3, None, 3),  # confirmed root, never converted
        (None, 3, 3),  # converted before the root was ever confirmed
        (5, 3, 3),  # both known — the EARLIER one opens the evidence window
        (3, 5, 3),
        (0, None, 0),  # a root at ply 0 is a boundary, not a missing one
    ],
)
def test_drill_boundary_is_the_earliest_known_signal(root_ply, rated_start_ply, expected):
    session = GameSession(
        session_mode="drill",
        drill_root_reached_ply=root_ply,
        rated_start_ply=rated_start_ply,
    )
    assert evidence_start_ply(session) == expected


def test_drill_without_root_or_conversion_has_no_boundary():
    # Neither signal exists, so nothing in the session can be told apart from scripted
    # route play. The answer is "no evidence", never "all of it".
    session = GameSession(session_mode="drill", drill_root_reached_ply=None, rated_start_ply=None)
    assert evidence_start_ply(session) is None
    assert split_evidence_hashes({"a": 0, "b": 9}, None) == split_evidence_hashes({}, 0)


# ---------------------------------------------------------------------------
# observed_position_plies — fen_before is an observation one ply EARLIER
# ---------------------------------------------------------------------------


def test_fen_before_is_dated_one_ply_earlier_than_its_row(db_session):
    game_session = _normal(db_session)
    db_session.commit()

    observations = observed_position_plies(db_session, session_id=game_session.id)

    # P0 exists ONLY as the first row's fen_before. Dating it at the row's own ply (1)
    # instead of ply 0 would push the whole pre-boundary prefix one ply later and leak
    # it back across a boundary of 1.
    assert observations[fen_hash(P0)] == 0
    assert observations[fen_hash(P1)] == 1
    assert observations[fen_hash(ROOT)] == 3
    assert observations[fen_hash(P5)] == 5


def test_repeated_position_keeps_its_latest_ply(db_session):
    game_session = _normal(db_session, moves=ROUTE_AND_PLAY + REVISIT_ROOT)
    db_session.commit()

    observations = observed_position_plies(db_session, session_id=game_session.id)

    # The root is seen at ply 3 and again at ply 7. Keeping the LATEST is what lets a
    # genuine transposition back into the root count as a reach later on.
    assert observations[fen_hash(ROOT)] == 7


def test_unparseable_fens_are_skipped_not_fatal(db_session):
    game_session = _normal(db_session, moves=[(1, "white", "not-a-fen", P1)])
    db_session.commit()

    observations = observed_position_plies(db_session, session_id=game_session.id)

    assert observations == {fen_hash(P1): 1}


# ---------------------------------------------------------------------------
# Runtime writer — the two roles in practice
# ---------------------------------------------------------------------------


def test_drill_scopes_broad_evidence_to_the_boundary(db_session):
    """Root is a SEED but not a REACH, and the scripted prefix seeds nothing."""
    blunders = _board(db_session)
    game_session = _drill(db_session)
    db_session.commit()

    events = _recompute(db_session, game_session)

    # The root still seeds the forward BFS, so what is genuinely downstream of it is
    # still an opportunity.
    assert events[blunders["downstream"].id] == (True, False)
    # But arriving at the root is the ROUTE's doing, not a ghost reach — and the root is
    # not forward-reachable from itself, so it earns no row at all.
    assert blunders["root"].id not in events
    # The pre-boundary route ply no longer seeds anything. This is the inflation the
    # boundary exists to remove: under whole-session hashing P1 was an opportunity
    # source and credited this blunder on every single drill.
    assert blunders["pre_route"].id not in events
    # ...and the ply-0 starting position is likewise pre-boundary here.
    assert blunders["start"].id not in events


def test_root_revisited_after_the_boundary_is_a_reach(db_session):
    blunders = _board(db_session)
    game_session = _drill(db_session, moves=ROUTE_AND_PLAY + REVISIT_ROOT)
    db_session.commit()

    events = _recompute(db_session, game_session)

    # Same position, but this time the player steered back into it under their own
    # power at ply 7. reached implies opportunity.
    assert events[blunders["root"].id] == (True, True)


def test_drill_without_a_boundary_contributes_nothing_and_retires_stale_rows(db_session):
    blunders = _board(db_session)
    game_session = _drill(db_session, drill_state="active", root_ply=None)
    # A row written before the boundary rule existed, on the very blunder the scripted
    # prefix used to credit.
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunders["pre_route"].id,
            session_id=game_session.id,
            occurred_at=game_session.started_at,
            opportunity=True,
            reached=False,
        )
    )
    db_session.commit()

    assert _recompute(db_session, game_session) == {}


def test_abandoned_drill_keeps_its_post_boundary_evidence(db_session):
    """The Again loop: 92% of drill sessions end 'abandoned' because that is the
    catch-all terminal bucket for "played to the strictness threshold, clicked Again".
    It measures engagement, not quitting, so terminal state must never scope evidence —
    only the boundary does.
    """
    blunders = _board(db_session)
    game_session = _drill(db_session, drill_state="abandoned")
    db_session.commit()

    events = _recompute(db_session, game_session)

    assert events[blunders["downstream"].id] == (True, False)
    assert blunders["pre_route"].id not in events


def test_conversion_boundary_applies_when_the_root_was_never_confirmed(db_session):
    blunders = _board(db_session)
    game_session = _drill(
        db_session, drill_state="converted", root_ply=None, rated_start_ply=3
    )
    db_session.commit()

    events = _recompute(db_session, game_session)

    # rated_start_ply alone is a real boundary: from conversion on this is ordinary
    # rated play, whatever the route did before it.
    assert events[blunders["downstream"].id] == (True, False)
    assert blunders["pre_route"].id not in events


def test_normal_session_evidence_is_unchanged(db_session):
    """The same moves in a normal game keep every observation, including ply 0."""
    blunders = _board(db_session)
    game_session = _normal(db_session)
    db_session.commit()

    events = _recompute(db_session, game_session)

    assert events[blunders["start"].id] == (True, True)  # ply 0, fen_before only
    assert events[blunders["root"].id] == (True, True)
    assert events[blunders["downstream"].id] == (True, False)
    # The position that the drill case correctly refuses: in a normal game the player
    # chose to be at P1, so what it can steer into IS an opportunity.
    assert events[blunders["pre_route"].id] == (True, False)


# ---------------------------------------------------------------------------
# Shared-helper parity with the historical backfill (g-boundary-backfill)
# ---------------------------------------------------------------------------


def test_reconstructed_boundary_matches_a_persisted_one(db_session):
    """One implementation, two callers.

    The backfill cannot read ``drill_root_reached_ply`` — that is the column it exists
    to populate — so it reconstructs the ply from the same observations and feeds it to
    ``split_evidence_hashes``. That path must land on exactly the sets the runtime
    writer gets once the column IS stamped, or the two would disagree about the same
    session at two different times.
    """
    _board(db_session)
    legacy = _drill(db_session, drill_state="root_reached", root_ply=None)
    stamped = _drill(db_session, drill_state="root_reached", root_ply=3)
    db_session.commit()

    reconstructed = split_evidence_hashes(
        observed_position_plies(db_session, session_id=legacy.id), 3
    )
    runtime = session_evidence_hashes(db_session, stamped)

    assert reconstructed == runtime
    # And the sets are the ones the roles describe, not two identical empties.
    assert reconstructed.seed == {fen_hash(ROOT), fen_hash(P4), fen_hash(P5)}
    assert reconstructed.reach == {fen_hash(P4), fen_hash(P5)}


# ---------------------------------------------------------------------------
# reached ⇒ opportunity
# ---------------------------------------------------------------------------


def test_reached_without_opportunity_is_rejected_by_the_schema(db_session):
    blunders = _board(db_session)
    game_session = _normal(db_session)
    db_session.commit()

    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunders["root"].id,
            session_id=game_session.id,
            occurred_at=game_session.started_at,
            opportunity=False,
            reached=True,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_opportunity_without_reach_is_still_allowed(db_session):
    # The implication is one-way. An 8-ply-neighbourhood opportunity that was never
    # arrived at is the ordinary shape of a broad row.
    blunders = _board(db_session)
    game_session = _normal(db_session)
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunders["root"].id,
            session_id=game_session.id,
            occurred_at=game_session.started_at,
            opportunity=True,
            reached=False,
        )
    )
    db_session.commit()

    assert _events(db_session, game_session)[blunders["root"].id] == (True, False)
