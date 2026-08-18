"""Observation-only opening/middlegame boundary protocol.

The first rollout stage records whether an active session *could* publish an
opening-score delta once its complete analyzed prefix reaches the Lichess
middlegame boundary.  It deliberately does not expose the prefix to opening
evidence, enqueue a recompute, poll a delta, or write ``opening_middle_ply``.

The raw FEN predicate is only a scheduling hint.  The authenticated proof route
replays the complete standard line and is the sole writer of an exact candidate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Protocol

from app.fen import normalize_fen
from app.game_phase import is_middlegame_position
from app.models import GameSession
from app.ply_coordinates import ply_after
from app.posthog_client import capture


OPENING_PHASE_PROTOCOL_VERSION = 1
OPENING_BOUNDARY_MAX_PROBE_PLY = 80


class OpeningBoundaryProbeVerdict(str, Enum):
    PASSED = "passed"
    WRONG_ROW_COUNT = "wrong_row_count"
    COORDINATE_MISMATCH = "coordinate_mismatch"
    NONSTANDARD_START = "nonstandard_start"
    ILLEGAL_OR_DISCONTINUOUS_LINE = "illegal_or_discontinuous_line"
    EXHAUSTED = "exhausted"
    CAPPED = "capped"


class _MoveHint(Protocol):
    move_number: int
    color: object
    fen_after: str


def _color_value(color: object) -> str:
    value = getattr(color, "value", color)
    return str(value)


def observe_raw_boundary_hint(
    session: GameSession,
    moves: Iterable[_MoveHint],
    *,
    protocol_version: int | None,
    allow_candidate_discovery: bool,
) -> None:
    """Stamp the explicit client protocol and a request-local raw FEN hint.

    Invalid FENs retain the upload endpoint's existing per-row behavior: they
    cannot become a hint, but this observation-only path never rejects the batch.
    Correctness does not depend on the hint being globally minimal; it only
    bounds when the browser asks the authoritative proof route to replay.
    """

    if protocol_version is None:
        return
    session.opening_phase_protocol_version = protocol_version
    if (
        not allow_candidate_discovery
        or session.status != "active"
        or session.opening_middle_candidate_ply is not None
        or session.opening_middle_ply is not None
        or session.opening_phase_exhausted
    ):
        return

    qualifying: list[int] = []
    for move in moves:
        try:
            normalized = normalize_fen(move.fen_after)
            if is_middlegame_position(normalized):
                qualifying.append(
                    ply_after(move.move_number, _color_value(move.color))
                )
        except (TypeError, ValueError):
            continue
    if not qualifying:
        return

    request_hint = min(qualifying)
    # Once a proof has started, retain the hint that proof was scheduled from.
    if session.opening_phase_probe_verdict is not None:
        return
    if (
        session.opening_phase_probe_ply is None
        or request_hint < session.opening_phase_probe_ply
    ):
        session.opening_phase_probe_ply = request_hint


def stamp_shadow_ready(session: GameSession, *, now: datetime | None = None) -> bool:
    """Stamp when an exact candidate and baseline first coexist while active."""

    if (
        session.status != "active"
        or session.opening_phase_exhausted
        or session.opening_middle_candidate_ply is None
        or session.opening_score_baseline is None
    ):
        return False
    if session.opening_middle_ready_at is None:
        session.opening_middle_ready_at = now or datetime.now(timezone.utc)
        return True
    return False


def clear_boundary_observation(session: GameSession) -> None:
    """Clear revision-specific observation state after an accepted takeback."""

    session.opening_phase_probe_ply = None
    session.opening_phase_probe_verdict = None
    session.opening_middle_candidate_ply = None
    session.opening_middle_ready_at = None
    session.opening_middle_ply = None
    session.opening_phase_exhausted = False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def opening_boundary_shadow_properties(
    session: GameSession,
    *,
    terminal_trigger: str,
    terminal_at: datetime,
) -> dict[str, object]:
    """Return the closed, content-free terminal observation projection."""

    raw_candidate_seen = bool(
        session.opening_phase_probe_ply is not None
        or session.opening_middle_candidate_ply is not None
        or session.opening_phase_probe_verdict is not None
        or session.opening_phase_exhausted
    )
    proof_verdict = session.opening_phase_probe_verdict or "not_attempted"
    baseline_ready = session.opening_score_baseline is not None
    would_have_published = bool(
        session.opening_phase_protocol_version == OPENING_PHASE_PROTOCOL_VERSION
        and session.opening_middle_candidate_ply is not None
        and session.opening_middle_ready_at is not None
        and baseline_ready
        and not session.opening_phase_exhausted
    )
    if not raw_candidate_seen:
        reason = "no_candidate"
    elif proof_verdict == "not_attempted":
        reason = "probe_ack_incomplete"
    elif proof_verdict in {
        OpeningBoundaryProbeVerdict.WRONG_ROW_COUNT.value,
        OpeningBoundaryProbeVerdict.COORDINATE_MISMATCH.value,
        OpeningBoundaryProbeVerdict.NONSTANDARD_START.value,
        OpeningBoundaryProbeVerdict.ILLEGAL_OR_DISCONTINUOUS_LINE.value,
        OpeningBoundaryProbeVerdict.CAPPED.value,
    }:
        reason = "continuity_or_cap_refusal"
    elif proof_verdict == OpeningBoundaryProbeVerdict.EXHAUSTED.value:
        reason = "exhausted"
    elif not baseline_ready or session.opening_middle_ready_at is None:
        reason = "baseline_missing"
    else:
        reason = "would_publish"

    lead_ms: int | None = None
    if session.opening_middle_ready_at is not None:
        lead_ms = max(
            0,
            round(
                (
                    _as_utc(terminal_at)
                    - _as_utc(session.opening_middle_ready_at)
                ).total_seconds()
                * 1000
            ),
        )

    return {
        "protocol_version": session.opening_phase_protocol_version,
        "session_mode": session.session_mode,
        "terminal_trigger": terminal_trigger,
        "raw_candidate_seen": raw_candidate_seen,
        "proof_verdict": proof_verdict,
        "baseline_ready_at_transition": baseline_ready,
        "would_have_published": would_have_published,
        "reason": reason,
        "line_revision_zero": session.move_line_revision == 0,
        "ready_to_terminal_lead_ms": lead_ms,
    }


def claim_opening_boundary_shadow_terminal(
    session: GameSession,
    *,
    terminal_at: datetime,
) -> bool:
    """Claim the session's first delta-bearing shadow transition durably."""

    if (
        session.opening_phase_protocol_version != OPENING_PHASE_PROTOCOL_VERSION
        or session.opening_boundary_shadow_terminal_at is not None
    ):
        return False
    session.opening_boundary_shadow_terminal_at = terminal_at
    return True


def emit_opening_boundary_shadow_terminal(
    session: GameSession,
    *,
    terminal_trigger: str,
    terminal_at: datetime,
) -> None:
    """Emit one aggregate-only observation beside a delta-bearing transition."""

    if session.opening_phase_protocol_version != OPENING_PHASE_PROTOCOL_VERSION:
        return
    capture(
        None,
        "opening_boundary_shadow_terminal",
        opening_boundary_shadow_properties(
            session,
            terminal_trigger=terminal_trigger,
            terminal_at=terminal_at,
        ),
    )
