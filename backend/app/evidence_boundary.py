"""The evidence boundary: which of a session's observed positions are SRS evidence.

A DRILL steers the player along a scripted route to an opening root. Those pre-root
plies are chosen by the server, not by the player, and both the product contract and
the steering path already treat them as non-evidence: ``next_opponent_move`` serves
pure route moves with ``target_blunder_id=None`` until the drill leaves its pre-root
state. The ACCOUNTING side used to disagree — it hashed every ``session_moves`` row
and seeded the opportunity BFS from all of them — so every ply of route walking
inflated dueness counters for every blunder in its 8-ply forward neighbourhood, and
routed arrival at the root was miscredited as a ghost REACH (g-ghost-preach-absorb).

This module is the single definition that closes that gap, shared by the runtime
writer (``app.api.session._compute_blunder_opportunity_events``) and the historical
backfill so a boundary can never be implemented twice and drift.

The boundary is a PLY, and it splits the session's observations into two roles:

* ``seed_hashes`` — observed AT OR AFTER the boundary. These seed the opponent-colour
  forward BFS, so the root position still contributes the opportunities that are
  genuinely downstream of it.
* ``reach_hashes`` — observed STRICTLY AFTER the boundary. Only these can mark a
  blunder ``reached``.

The root is therefore a seed but NOT a reach: arriving there is the route's doing.
Because observations keep the LATEST ply per FEN, a later repetition of the root — a
transposition the player actually steered back into — does count as a reach.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy.orm import Session

from app.fen import fen_hash
from app.models import GameSession, SessionMove
from app.session_contracts import NORMAL_SESSION_MODE, ply_after

# A normal game is evidence from its very first position. -1 rather than 0 because the
# reach rule is STRICTLY greater than the boundary, and ply 0 — the starting position,
# carried by the first row's ``fen_before`` — must remain a reach exactly as it was
# before boundaries existed.
NORMAL_SESSION_EVIDENCE_START_PLY = -1


@dataclass(frozen=True)
class EvidenceHashes:
    """The two observation roles, as normalized FEN hashes.

    ``reach`` is always a subset of ``seed``: observations are deduped to their latest
    ply, so a hash after the boundary is also at-or-after it. Callers that need every
    hash to resolve to a ``positions`` row can therefore query ``seed`` alone.
    """

    seed: frozenset[str]
    reach: frozenset[str]

    def __bool__(self) -> bool:
        return bool(self.seed)


@dataclass(frozen=True)
class ObservedPlyBounds:
    """Earliest and latest ply at which one normalized position was observed.

    Runtime evidence uses :attr:`latest` so a genuine post-boundary repetition counts
    as a reach. The legacy boundary repair uses :attr:`earliest` so a later
    transposition cannot move the historical root forward. Keeping both views over one
    scan prevents their FEN parsing and ply arithmetic from drifting.
    """

    earliest: int
    latest: int


def evidence_start_ply(session: GameSession) -> int | None:
    """The ply before which this session's observations are not SRS evidence.

    * Normal session → :data:`NORMAL_SESSION_EVIDENCE_START_PLY` (everything counts).
    * Drill with a confirmed root and/or a rated conversion → the EARLIER of the two.
      Conversion can precede confirmation (a player may continue from a stopped drill
      before the root is ever proven), and a converted drill is ordinary rated play
      from that ply on, so taking the minimum keeps genuine post-boundary evidence
      that either signal alone would discard.
    * Drill with neither → ``None``: no boundary was ever established, so nothing in
      the session can be distinguished from scripted route play. It contributes NO
      broad evidence. That is a real, expected residue for legacy and soft-declined
      sessions (see ``GameSession.drill_root_reached_ply``), not a defect — its
      targeted attempts survive independently in ``opponent_decisions``.
    """
    if session.session_mode == NORMAL_SESSION_MODE:
        return NORMAL_SESSION_EVIDENCE_START_PLY
    candidates = [
        ply
        for ply in (session.drill_root_reached_ply, session.rated_start_ply)
        if ply is not None
    ]
    return min(candidates) if candidates else None


def observed_position_ply_bounds(
    db: Session, *, session_id: uuid.UUID
) -> dict[str, ObservedPlyBounds]:
    """Earliest/latest observed plies for every normalized FEN hash in a session.

    BOTH ``fen_before`` and ``fen_after`` are observations, at ``ply-1`` and ``ply``
    respectively — dropping ``fen_before`` would silently discard the starting
    position and every opponent-to-move position the session passed through, and
    hashing it at the row's own ply would date a pre-boundary position one ply late
    and leak it back into the evidence set.

    Unparseable FENs are skipped, which is how a corrupted row retires its events
    rather than poisoning the set.
    """
    rows = (
        db.query(
            SessionMove.move_number,
            SessionMove.color,
            SessionMove.fen_before,
            SessionMove.fen_after,
        )
        .filter(SessionMove.session_id == session_id)
        .all()
    )
    bounds: dict[str, ObservedPlyBounds] = {}
    for move_number, color, fen_before, fen_after in rows:
        ply = ply_after(move_number, color)
        for fen, observed_ply in ((fen_before, ply - 1), (fen_after, ply)):
            if not fen:
                continue
            try:
                hashed = fen_hash(fen)
            except ValueError:
                continue
            current = bounds.get(hashed)
            if current is None:
                bounds[hashed] = ObservedPlyBounds(
                    earliest=observed_ply,
                    latest=observed_ply,
                )
            else:
                bounds[hashed] = ObservedPlyBounds(
                    earliest=min(current.earliest, observed_ply),
                    latest=max(current.latest, observed_ply),
                )
    return bounds


def observed_position_plies(db: Session, *, session_id: uuid.UUID) -> dict[str, int]:
    """Latest ply at which each normalized FEN hash was observed in this session.

    Keeping the LATEST ply per hash is what makes a repetition count: the root is a
    seed at the boundary, but a later transposition back into it is a genuine reach.
    """
    return {
        hashed: observed.latest
        for hashed, observed in observed_position_ply_bounds(
            db, session_id=session_id
        ).items()
    }


def split_evidence_hashes(
    observations: dict[str, int], boundary_ply: int | None
) -> EvidenceHashes:
    """Split observations into BFS seeds and reach candidates around ``boundary_ply``.

    ``None`` means no boundary was ever established, which yields both roles empty —
    "we cannot tell scripted route play from real play here" is answered with no
    evidence at all, never with all of it.
    """
    if boundary_ply is None:
        return EvidenceHashes(seed=frozenset(), reach=frozenset())
    return EvidenceHashes(
        seed=frozenset(h for h, ply in observations.items() if ply >= boundary_ply),
        reach=frozenset(h for h, ply in observations.items() if ply > boundary_ply),
    )


def session_evidence_hashes(db: Session, session: GameSession) -> EvidenceHashes:
    """Convenience composition of the three steps above for a live session row."""
    return split_evidence_hashes(
        observed_position_plies(db, session_id=session.id),
        evidence_start_ply(session),
    )
