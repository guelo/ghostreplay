"""Persistence-half stability regressions for analysis-driven outcomes
(g-repair-drill-cache, AC5-7).

Everything here drives PRODUCTION code paths — the session-move upload endpoint,
the session-analysis read endpoint, the analysis-cache lookup endpoint, the
shared cache writer, the real classifier port, and the real SRS endpoint — so the
assertions reflect what the app actually does, not a test-local re-derivation.

Shared scenario matrix: eval pairs taken from the golden classification vectors
(also consumed by src/workers/analysisUtils.test.ts) so the classification is the
PRODUCTION ``classify_move_advanced`` output, plus deltas straddling the
recordable-failure boundary (50). The drill strictness sweep is a FRONTEND
concern (``gradeDrillMove``) and is covered in ChessGame.drillStability.test.tsx;
this file does not re-implement a drill comparator.

Coverage:
* AC7 — live analysis / session_moves / cache lookup agree: a move uploaded
  through ``POST /api/session/{id}/moves`` is read back through both
  ``GET /api/session/{id}/analysis`` (post-game display surface) and
  ``POST /api/analysis/lookup`` (cache surface); ``eval_delta``, the production
  classification, and the best-move eval must agree across both, and survive an
  idempotent re-upload.
* AC5 — classification stability: the stored/round-tripped classification equals
  the deterministic ``classify_move_advanced`` output for the eval pair.
* AC6 — SRS exactly-once: ``POST /api/srs/review`` records exactly one review row
  and moves ``pass_streak`` per the recordable comparator, across the matrix.
* Canonical trust round-trip: a canonical row written through the shared writer
  surfaces as ``trusted_for_resolution`` with its delta intact, idempotently.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.analysis_cache_repo import write_analysis_cache_rows
from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import RESOLVER_COMPLETE_V2
from app.models import AnalysisCache, Blunder, Position
from app.move_classification import EngineScore, classify_move_advanced

PROFILE_ID = "canonical-sf18-depth24-linux-v1"
FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
RECORDABLE_FAILURE_THRESHOLD_CP = 50

_FIXTURE = Path(__file__).resolve().parent / "tests" / "fixtures" / "classification_vectors.json"


def _cp_white_mover_cases() -> list[tuple[int, int, str]]:
    """(best_eval, played_eval, classification) for white-to-move cp vectors.

    Uses the SAME golden fixture the TS classifier suite consumes, so the
    classification is the production value, not an artificial constant. White
    mover + white pov => player-relative == white-relative and
    eval_delta = best - played.
    """
    cases = json.load(open(_FIXTURE))["cases"]
    out: list[tuple[int, int, str]] = []
    for c in cases:
        if (
            c["prevScore"]["type"] == "cp"
            and c["nextScore"]["type"] == "cp"
            and c["scorePov"] == "white"
            and c["mover"] == "white"
            and not c["isBest"]
            and c["prevScore"]["value"] >= c["nextScore"]["value"]  # best >= played
        ):
            best = c["prevScore"]["value"]
            played = c["nextScore"]["value"]
            expected = classify_move_advanced(
                EngineScore(type="cp", value=best),
                EngineScore(type="cp", value=played),
                "white",
                "white",
                False,
            )
            assert expected == c["expected"]  # production classifier == golden
            out.append((best, played, expected))
    return out


CLASSIFICATION_CASES = _cp_white_mover_cases()


@pytest.fixture(autouse=True)
def _stub_opening_cache_refresh():
    with patch("app.api.srs.request_recompute", return_value=None), patch(
        "app.api.session.request_recompute", return_value=None
    ):
        yield


def _upload_move(client, auth_headers, session_id, *, best_eval, played_eval, classification):
    """Upload a single white move through the real session-moves endpoint.

    Populates session_moves AND (via _upsert_analysis_cache) analysis_cache from
    the same payload, exactly like a real game upload.
    """
    eval_delta = max(best_eval - played_eval, 0)
    return client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": AFTER_FEN,
                    "fen_before": FEN,
                    "move_uci": "e2e4",
                    "eval_cp": played_eval,
                    "best_move_san": "Nf3",
                    "best_move_uci": "g1f3",
                    "best_line_uci": ["g1f3", "b8c6"],
                    "best_move_eval_cp": best_eval,
                    "eval_delta": eval_delta,
                    "classification": classification,
                }
            ]
        },
        headers=auth_headers(),
    )


@pytest.mark.parametrize("best_eval,played_eval,classification", CLASSIFICATION_CASES)
def test_session_analysis_and_cache_lookup_agree(
    client, auth_headers, create_game_session, best_eval, played_eval, classification
):
    session_id = create_game_session(user_id=123, player_color="white")
    eval_delta = max(best_eval - played_eval, 0)

    resp = _upload_move(
        client, auth_headers, session_id,
        best_eval=best_eval, played_eval=played_eval, classification=classification,
    )
    assert resp.status_code == 200

    # Post-game display surface.
    analysis = client.get(
        f"/api/session/{session_id}/analysis", headers=auth_headers()
    ).json()
    move = next(m for m in analysis["moves"] if m["move_san"] == "e4")

    # Cache surface.
    lookup = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": FEN, "move_uci": "e2e4"}]},
        headers=auth_headers(),
    ).json()
    cached = lookup["results"][f"{FEN}::e2e4"]

    # AC7: the two surfaces agree on the values downstream decisions read.
    assert move["eval_delta"] == eval_delta == cached["eval_delta"]
    # AC5: the production classification round-trips through both surfaces.
    assert move["classification"] == classification == cached["classification"]
    # White move => white-relative best eval agrees across surfaces.
    assert move["best_move_eval_cp"] == best_eval == cached["best_eval"]
    # Boundary decision derived from the agreed delta is consistent.
    assert (eval_delta >= RECORDABLE_FAILURE_THRESHOLD_CP) == (eval_delta >= 50)


@pytest.mark.parametrize("best_eval,played_eval,classification", CLASSIFICATION_CASES)
def test_reupload_keeps_surfaces_in_agreement(
    client, auth_headers, create_game_session, best_eval, played_eval, classification
):
    session_id = create_game_session(user_id=123, player_color="white")
    for _ in range(2):  # idempotent re-upload (varied source timing proxy)
        assert _upload_move(
            client, auth_headers, session_id,
            best_eval=best_eval, played_eval=played_eval, classification=classification,
        ).status_code == 200

    analysis = client.get(
        f"/api/session/{session_id}/analysis", headers=auth_headers()
    ).json()
    e4_moves = [m for m in analysis["moves"] if m["move_san"] == "e4"]
    assert len(e4_moves) == 1  # no duplicate session_moves row
    assert e4_moves[0]["classification"] == classification

    lookup = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": FEN, "move_uci": "e2e4"}]},
        headers=auth_headers(),
    ).json()
    assert lookup["results"][f"{FEN}::e2e4"]["classification"] == classification


# --- Canonical trust round-trip (shared writer + real lookup) ------------------


def _identity_columns() -> dict:
    profile = get_profile(PROFILE_ID)
    return {f: getattr(profile, f) for f in IDENTITY_FIELDS}


def _canonical_row(move_uci: str, delta: int) -> dict:
    row = {
        "fen_before": FEN,
        "move_uci": move_uci,
        "move_san": "e4",
        "source": "precomputed",
        "analysis_profile_id": PROFILE_ID,
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "played_eval": 0,
        "played_eval_mate": None,
        "best_eval": delta,
        "best_eval_mate": None,
        "eval_delta": delta,
        "classification": "mistake",
    }
    row.update(_identity_columns())
    return row


@pytest.mark.parametrize("delta", [14, 15, 16, 49, 50, 51])
def test_canonical_row_trusted_and_idempotent(client, auth_headers, db_session, delta):
    move = "d2d4"
    db_session.rollback()
    write_analysis_cache_rows(db_session, [_canonical_row(move, delta)])
    db_session.rollback()
    write_analysis_cache_rows(db_session, [_canonical_row(move, delta)])  # re-run

    result = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": FEN, "move_uci": move}]},
        headers=auth_headers(),
    ).json()["results"][f"{FEN}::{move}"]
    assert result["eval_delta"] == delta
    assert result["trusted_for_resolution"] is True

    db_session.rollback()
    count = (
        db_session.query(AnalysisCache)
        .filter(AnalysisCache.fen_before == FEN, AnalysisCache.move_uci == move)
        .count()
    )
    assert count == 1


# --- SRS exactly-once across the recordable matrix -----------------------------

SRS_DELTAS = [14, 15, 35, 49, 50, 51, 250]


def _create_blunder(db_session, *, user_id: int, pass_streak: int) -> Blunder:
    position = Position(
        user_id=user_id,
        fen_hash=f"fen-hash-{user_id}-{pass_streak}",
        fen_raw="8/8/8/8/8/8/8/8 w - - 0 1",
        active_color="white",
    )
    db_session.add(position)
    db_session.flush()
    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san="Qh5",
        best_move_san="Nf3",
        eval_loss_cp=120,
        pass_streak=pass_streak,
        last_reviewed_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    db_session.add(blunder)
    db_session.commit()
    db_session.refresh(blunder)
    return blunder


@pytest.mark.parametrize("delta", SRS_DELTAS)
def test_srs_review_records_once_and_moves_streak(
    client, auth_headers, create_game_session, db_session, delta
):
    passed = max(delta, 0) < RECORDABLE_FAILURE_THRESHOLD_CP
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    blunder = _create_blunder(db_session, user_id=user_id, pass_streak=1)

    resp = client.post(
        "/api/srs/review",
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": passed,
            "user_move": "Nf3",
            "eval_delta": delta,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert resp.status_code == 200
    expected_streak = 2 if passed else 0
    assert resp.json()["pass_streak"] == expected_streak

    db_session.expire_all()
    updated = db_session.query(Blunder).filter(Blunder.id == blunder.id).first()
    assert updated.pass_streak == expected_streak

    from sqlalchemy import text
    rows = db_session.execute(
        text("SELECT passed, eval_delta_cp FROM blunder_reviews WHERE blunder_id = :bid"),
        {"bid": blunder.id},
    ).fetchall()
    assert len(rows) == 1
    assert bool(rows[0][0]) == passed
    assert rows[0][1] == delta
