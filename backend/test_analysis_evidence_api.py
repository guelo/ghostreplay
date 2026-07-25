"""Tests for the session-scoped analysis-evidence endpoint and its helpers
(g-cache-stronger-evals).

The endpoint persists complete depth-21 browser-analysis evidence through the
shared cache writer: owner-only, scoped to exact mainline moves, stamped with the
non-authoritative-but-replacement-eligible ``browser-analysis-v1`` profile.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import chess
import pytest
from pydantic import ValidationError

from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    get_profile,
    stamp_profile_full,
)
from app.api.session import (
    EVIDENCE_CLASSIFICATION_MISMATCH,
    EVIDENCE_CONTRACT_UNSATISFIED,
    EVIDENCE_DUPLICATE_KEY,
    EVIDENCE_INVALID_LEGALITY,
    EVIDENCE_NOT_IN_SESSION,
    EVIDENCE_SESSION_NOT_ELIGIBLE,
    EVIDENCE_WRITER_NO_RESULT,
    AnalysisEvidenceRow,
    _build_evidence_cache_row,
    _derive_move_uci,
    _EVIDENCE_ACCEPTED_REASONS,
    _prepare_analysis_evidence_rows,
    _session_membership_keys,
)
from app.evidence_contracts import RESOLVER_COMPLETE_V2
from app.fen import normalize_fen
from app.models import AnalysisCache, GameSession, SessionMove

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
FEN_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
FEN_AFTER_E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _add_session_move(
    db, session_id, *, move_number, color, move_san, fen_before, fen_after,
    classification=None,
):
    db.add(
        SessionMove(
            session_id=uuid.UUID(session_id),
            move_number=move_number,
            color=color,
            move_san=move_san,
            fen_before=fen_before,
            fen_after=fen_after,
            classification=classification,
            segment="normal",
        )
    )
    db.commit()


def _seed_browser_analysis_row(
    db, *, fen, move_uci, move_san="e4", best_move_uci="e2e4", best_move_san="e4",
    best_line_uci="e2e4 e7e5", played_eval=40, best_eval=40, eval_delta=0,
    classification="best", played_eval_mate=None, best_eval_mate=None,
    profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
):
    """Seed an eligible, identity-stamped analysis row (resolver-complete-v2) — the
    durable evidence Part C overlays and a same-profile merge/idempotent partner for
    Part B. Defaults to the active browser-analysis-multipv-v2 successor; pass
    ``profile=BROWSER_ANALYSIS_PROFILE_ID`` to seed a RETIRED hidden-analysis row
    (still identity-verified, still DISPLAY_OVERLAY, correctively replaceable)."""
    data = dict(
        fen_before=fen,
        normalized_fen_before=normalize_fen(fen),
        move_uci=move_uci,
        move_san=move_san,
        best_move_uci=best_move_uci,
        best_move_san=best_move_san,
        best_line_uci=best_line_uci,
        played_eval=played_eval,
        played_eval_mate=played_eval_mate,
        best_eval=best_eval,
        best_eval_mate=best_eval_mate,
        eval_delta=eval_delta,
        classification=classification,
        source="analysis",
        analysis_profile_id=profile,
        evidence_contract_id=RESOLVER_COMPLETE_V2,
    )
    data.update(stamp_profile_full(profile))
    row = AnalysisCache(**data)
    db.add(row)
    db.commit()
    return row


def _seed_canonical_row(db, *, fen, move_uci, **overrides):
    """Seed an identity-stamped canonical (authoritative) resolver-complete-v2 row."""
    data = dict(
        fen_before=fen,
        normalized_fen_before=normalize_fen(fen),
        move_uci=move_uci,
        move_san=overrides.get("move_san", "e4"),
        best_move_uci=overrides.get("best_move_uci", "e2e4"),
        best_move_san=overrides.get("best_move_san", "e4"),
        best_line_uci=overrides.get("best_line_uci", "e2e4 e7e5"),
        played_eval=overrides.get("played_eval", 20),
        best_eval=overrides.get("best_eval", 20),
        eval_delta=overrides.get("eval_delta", 0),
        classification=overrides.get("classification", "best"),
        source="precomputed",
        analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id=RESOLVER_COMPLETE_V2,
    )
    data.update(stamp_profile_full(CANONICAL_PROFILE_ID))
    row = AnalysisCache(**data)
    db.add(row)
    db.commit()
    return row


def _seed_e4_move(db, session_id):
    """The canonical mainline move used by most tests: 1. e4 from the start."""
    _add_session_move(
        db, session_id, move_number=1, color="white", move_san="e4",
        fen_before=START, fen_after=FEN_AFTER_E4,
    )


def _evidence_row(
    *,
    fen=START,
    move_uci="e2e4",
    best_move_uci="e2e4",
    best_line_uci=("e2e4", "e7e5"),
    played_eval=30,
    best_eval=30,
    eval_delta=0,
    classification="best",
    played_eval_mate=None,
    best_eval_mate=None,
):
    return {
        "fen": fen,
        "move_uci": move_uci,
        "best_move_uci": best_move_uci,
        "best_line_uci": list(best_line_uci),
        "played_eval": played_eval,
        "best_eval": best_eval,
        "eval_delta": eval_delta,
        "classification": classification,
        "played_eval_mate": played_eval_mate,
        "best_eval_mate": best_eval_mate,
    }


def _post(client, auth_headers, session_id, rows, user_id=123, producer="visible-multipv-v1"):
    body: dict = {"rows": rows}
    if producer is not _NO_PRODUCER:
        body["producer"] = producer
    return client.post(
        f"/api/session/{session_id}/analysis-evidence",
        json=body,
        headers=auth_headers(user_id=user_id),
    )


# Sentinel so a test can post with NO producer field at all (stale-client path),
# distinct from producer=None which would still send "producer": null.
_NO_PRODUCER = object()


def _seed_browser_game_row(db, *, fen, move_uci, played_eval=25, best_eval=25, **overrides):
    row = AnalysisCache(
        fen_before=fen,
        normalized_fen_before=normalize_fen(fen),
        move_uci=move_uci,
        move_san=overrides.get("move_san", "e4"),
        best_move_uci=overrides.get("best_move_uci", "e2e4"),
        best_move_san=overrides.get("best_move_san", "e4"),
        best_line_uci=overrides.get("best_line_uci", "e2e4 e7e5"),
        played_eval=played_eval,
        best_eval=best_eval,
        eval_delta=overrides.get("eval_delta", 0),
        classification=overrides.get("classification", "best"),
        source="game",
        analysis_profile_id=BROWSER_PROFILE_ID,
        evidence_contract_id="resolver-complete-v1",
    )
    db.add(row)
    db.commit()
    return row


def _cache_row(db, fen, move_uci):
    db.expire_all()
    return (
        db.query(AnalysisCache)
        .filter(AnalysisCache.fen_before == fen, AnalysisCache.move_uci == move_uci)
        .first()
    )


def _make_drill(db, session_id, *, drill_state, status="active"):
    gs = db.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    gs.session_mode = "drill"
    gs.drill_state = drill_state
    gs.status = status
    if drill_state == "converted":
        gs.is_rated = True
        gs.normal_started_at = datetime.now(timezone.utc)
        gs.converted_at = datetime.now(timezone.utc)
        gs.rated_start_ply = 0
    else:
        gs.is_rated = False
        gs.rated_start_ply = None
    db.commit()


# --------------------------------------------------------------------------- #
# _derive_move_uci — SAN->UCI derivation at the divergence points
# --------------------------------------------------------------------------- #
def test_derive_move_uci_standard():
    assert _derive_move_uci(START, "e4") == "e2e4"


def test_derive_move_uci_castling():
    fen = "rnbqk2r/pppp1ppp/5n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"
    assert _derive_move_uci(fen, "O-O") == "e1g1"


def test_derive_move_uci_promotion():
    fen = "8/P7/8/8/8/8/8/k6K w - - 0 1"
    assert _derive_move_uci(fen, "a8=Q") == "a7a8q"


def test_derive_move_uci_en_passant():
    fen = "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3"
    assert _derive_move_uci(fen, "exf6") == "e5f6"


def test_derive_move_uci_null_and_unparseable():
    assert _derive_move_uci(None, "e4") is None
    assert _derive_move_uci(START, None) is None
    assert _derive_move_uci("not a fen", "e4") is None
    assert _derive_move_uci(START, "Zz9") is None  # illegal SAN


def test_derive_matches_chessjs_uci_convention():
    # The browser-game upload stored a chess.js-derived UCI; python-chess must agree
    # at the realistic edge cases for the exact-key replacement to land.
    for fen, san, expected in [
        (START, "e4", "e2e4"),
        ("rnbqk2r/pppp1ppp/5n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4", "O-O", "e1g1"),
        ("8/P7/8/8/8/8/8/k6K w - - 0 1", "a8=Q", "a7a8q"),
        ("rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3", "exf6", "e5f6"),
    ]:
        assert _derive_move_uci(fen, san) == expected


# --------------------------------------------------------------------------- #
# _session_membership_keys / _build_evidence_cache_row (pure helpers)
# --------------------------------------------------------------------------- #
def test_membership_keys_skip_legacy_null_fen():
    moves = [
        SessionMove(session_id=uuid.uuid4(), move_number=1, color="white", move_san="e4", fen_before=START, fen_after=FEN_AFTER_E4),
        SessionMove(session_id=uuid.uuid4(), move_number=1, color="black", move_san="e5", fen_before=None, fen_after=FEN_AFTER_E5),
    ]
    assert _session_membership_keys(moves) == {(START, "e2e4")}


def test_build_evidence_cache_row_stamps_profile_and_derives_san():
    row = AnalysisEvidenceRow(**_evidence_row())
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert reason is None
    assert cache_row["analysis_profile_id"] == BROWSER_ANALYSIS_MULTIPV_PROFILE_ID
    assert cache_row["source"] == "analysis"
    assert cache_row["evidence_contract_id"] == "resolver-complete-v2"
    # SAN derived server-side from the validated UCI.
    assert cache_row["move_san"] == "e4"
    assert cache_row["best_move_san"] == "e4"
    # Full pinned identity stamped so the row identity-verifies.
    p = get_profile(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert cache_row["engine_build"] == p.engine_build
    assert cache_row["eval_file_id"] == p.eval_file_id


def test_build_evidence_cache_row_rejects_illegal_best_move():
    row = AnalysisEvidenceRow(**_evidence_row(best_move_uci="e2e5"))  # illegal from start
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert cache_row is None
    assert reason == EVIDENCE_INVALID_LEGALITY


def test_build_evidence_cache_row_rejects_illegal_pv():
    row = AnalysisEvidenceRow(**_evidence_row(best_line_uci=("e2e4", "e2e4")))  # 2nd ply illegal
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert cache_row is None
    assert reason == EVIDENCE_INVALID_LEGALITY


# --------------------------------------------------------------------------- #
# _build_evidence_cache_row — classification rederivation POV + mate coverage
#
# The stored evals are white-relative; the root classifier needs ROOT
# side-to-move-relative scores. These pin the black-to-move POV flip and the
# mate transitions, which the white-CP endpoint tests above never exercise. The
# evals mirror the frontend encoding (analysisEvidence.ts): mate -> mateToCp
# (MATE_BASE_CP=10000, decay 10), then white-relative sign flip.
# --------------------------------------------------------------------------- #
def _black_row(**overrides):
    """A black-to-move evidence row: after 1.e4, best e7e5, an inferior a7a6."""
    base = dict(
        fen=FEN_AFTER_E4,
        move_uci="a7a6",
        best_move_uci="e7e5",
        best_line_uci=("e7e5", "g1f3"),
    )
    base.update(overrides)
    return _evidence_row(**base)


def test_build_evidence_cache_row_black_root_normalizes_pov():
    # White-relative: best -40 (black better), played +60 (white better). For BLACK
    # the correct root-relative drop is an 'inaccuracy'. If the endpoint failed to
    # flip POV it would derive 'excellent' and reject this row — so acceptance here
    # is what proves the black normalization.
    row = AnalysisEvidenceRow(
        **_black_row(
            played_eval=60, best_eval=-40, eval_delta=100, classification="inaccuracy"
        )
    )
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert reason is None
    assert cache_row is not None
    assert cache_row["classification"] == "inaccuracy"
    assert cache_row["move_san"] == "a6"
    assert cache_row["best_move_san"] == "e5"


def test_build_evidence_cache_row_black_root_rejects_white_pov_label():
    # Same black-to-move scores mislabeled with the un-flipped (white-POV) verdict
    # 'excellent' -> rederivation catches the POV error and rejects.
    row = AnalysisEvidenceRow(
        **_black_row(
            played_eval=60, best_eval=-40, eval_delta=100, classification="excellent"
        )
    )
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert cache_row is None
    assert reason == EVIDENCE_CLASSIFICATION_MISMATCH


def test_build_evidence_cache_row_black_root_mate_lost_is_blunder():
    # Black had mate-in-2 (white-relative best_eval -9980, best_eval_mate -2) but the
    # played move drops to a mere +50 for black (white-relative -50). Root-relative
    # this is a MateLost -> 'blunder'. Pins the mate-transition branch for black.
    row = AnalysisEvidenceRow(
        **_black_row(
            best_eval=-9980, best_eval_mate=-2,
            played_eval=-50, played_eval_mate=None,
            eval_delta=9930, classification="blunder",
        )
    )
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert reason is None
    assert cache_row is not None
    assert cache_row["classification"] == "blunder"
    assert cache_row["best_eval_mate"] == -2


def test_build_evidence_cache_row_black_root_mate_rejects_wrong_label():
    # The same MateLost mislabeled 'excellent' (what an un-flipped POV would yield)
    # is rejected.
    row = AnalysisEvidenceRow(
        **_black_row(
            best_eval=-9980, best_eval_mate=-2,
            played_eval=-50, played_eval_mate=None,
            eval_delta=9930, classification="excellent",
        )
    )
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert cache_row is None
    assert reason == EVIDENCE_CLASSIFICATION_MISMATCH


def test_build_evidence_cache_row_white_root_mate_lost_is_blunder():
    # White-to-move mate transition: white had mate-in-2 (best_eval 9980,
    # best_eval_mate 2) but the played move drops to +50 -> MateLost 'blunder'.
    row = AnalysisEvidenceRow(
        **_evidence_row(
            move_uci="e2e4", best_move_uci="d2d4", best_line_uci=("d2d4", "d7d5"),
            best_eval=9980, best_eval_mate=2,
            played_eval=50, played_eval_mate=None,
            eval_delta=9930, classification="blunder",
        )
    )
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert reason is None
    assert cache_row is not None
    assert cache_row["classification"] == "blunder"


def test_evidence_row_caps_best_line_length():
    # best_line_uci is bounded at 64 plies so a long legal shuffle line cannot
    # force unbounded per-ply legality generation. 64 is well above real PVs
    # (≲40) and accepted; 65 is rejected at the model layer (a 422 at the wire).
    AnalysisEvidenceRow(**_evidence_row(best_line_uci=tuple(["e2e4"] * 64)))
    with pytest.raises(ValidationError):
        AnalysisEvidenceRow(**_evidence_row(best_line_uci=tuple(["e2e4"] * 65)))


def test_build_evidence_cache_row_rejects_sparse_contract():
    row = AnalysisEvidenceRow(**_evidence_row(played_eval=None))
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert cache_row is None
    assert reason == EVIDENCE_CONTRACT_UNSATISFIED


def test_build_evidence_cache_row_rejects_inconsistent_delta():
    # White to move: expected delta = best - played = 70, submitted 5 -> rejected.
    row = AnalysisEvidenceRow(**_evidence_row(played_eval=30, best_eval=100, eval_delta=5))
    cache_row, reason = _build_evidence_cache_row(row, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert cache_row is None
    assert reason == EVIDENCE_CONTRACT_UNSATISFIED


def test_prepare_dedupes_and_flags_not_in_session():
    membership = {(START, "e2e4")}
    rows = [
        AnalysisEvidenceRow(**_evidence_row()),
        AnalysisEvidenceRow(**_evidence_row()),  # duplicate key
        AnalysisEvidenceRow(**_evidence_row(fen=FEN_AFTER_E4, move_uci="e7e5")),  # not in session
    ]
    prepared = _prepare_analysis_evidence_rows(rows, membership, "visible-multipv-v1")
    assert prepared[0].reason is None and prepared[0].cache_row is not None
    assert prepared[1].reason == EVIDENCE_DUPLICATE_KEY
    assert prepared[2].reason == EVIDENCE_NOT_IN_SESSION


# --------------------------------------------------------------------------- #
# endpoint: authorization & membership
# --------------------------------------------------------------------------- #
def test_endpoint_404_missing_session(client, auth_headers):
    resp = _post(client, auth_headers, str(uuid.uuid4()), [_evidence_row()])
    assert resp.status_code == 404


def test_endpoint_403_non_owner(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    resp = _post(client, auth_headers, session_id, [_evidence_row()], user_id=999)
    assert resp.status_code == 403


def test_endpoint_not_in_session(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    # A real legal move that is NOT in the session mainline.
    row = _evidence_row(fen=FEN_AFTER_E4, move_uci="e7e5", best_move_uci="e7e5",
                        best_line_uci=("e7e5", "g1f3"))
    resp = _post(client, auth_headers, session_id, [row])
    assert resp.status_code == 200
    assert resp.json()["results"][0]["reason"] == EVIDENCE_NOT_IN_SESSION


def test_endpoint_legacy_null_fen_move_not_eligible(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    # Session move stored with a null fen_before -> not in membership.
    _add_session_move(db_session, session_id, move_number=1, color="white",
                      move_san="e4", fen_before=None, fen_after=FEN_AFTER_E4)
    resp = _post(client, auth_headers, session_id, [_evidence_row()])
    assert resp.json()["results"][0]["reason"] == EVIDENCE_NOT_IN_SESSION


# --------------------------------------------------------------------------- #
# endpoint: session-eligibility gate
# --------------------------------------------------------------------------- #
def test_endpoint_abandoned_drill_not_eligible(client, auth_headers, create_game_session, db_session, monkeypatch):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _make_drill(db_session, session_id, drill_state="abandoned", status="ended")

    writer = MagicMock()
    monkeypatch.setattr("app.api.session.write_analysis_cache_rows", writer)
    resp = _post(client, auth_headers, session_id, [_evidence_row()])
    assert resp.json()["results"][0]["reason"] == EVIDENCE_SESSION_NOT_ELIGIBLE
    writer.assert_not_called()


def test_endpoint_ended_unconverted_drill_not_eligible(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _make_drill(db_session, session_id, drill_state="failed", status="ended")
    resp = _post(client, auth_headers, session_id, [_evidence_row()])
    assert resp.json()["results"][0]["reason"] == EVIDENCE_SESSION_NOT_ELIGIBLE
    assert _cache_row(db_session, START, "e2e4") is None


def test_endpoint_converted_drill_is_eligible(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _make_drill(db_session, session_id, drill_state="converted")
    resp = _post(client, auth_headers, session_id, [_evidence_row()])
    assert resp.json()["results"][0]["reason"] == "new_key"
    assert _cache_row(db_session, START, "e2e4") is not None


def test_endpoint_active_drill_is_eligible(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _make_drill(db_session, session_id, drill_state="active", status="active")
    resp = _post(client, auth_headers, session_id, [_evidence_row()])
    assert resp.json()["results"][0]["reason"] == "new_key"


def test_endpoint_gate_delegates_to_shared_predicate(client, auth_headers, create_game_session, db_session, monkeypatch):
    # Assert the endpoint uses the SAME predicate as upsert_session_moves rather than
    # re-implementing it: forcing the shared predicate False rejects a normal session.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    monkeypatch.setattr("app.api.session._should_run_session_move_evidence", lambda gs: False)
    resp = _post(client, auth_headers, session_id, [_evidence_row()])
    assert resp.json()["results"][0]["reason"] == EVIDENCE_SESSION_NOT_ELIGIBLE


# --------------------------------------------------------------------------- #
# endpoint: writes
# --------------------------------------------------------------------------- #
def test_endpoint_inserts_new_key_with_stamped_identity(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    resp = _post(client, auth_headers, session_id, [_evidence_row()])
    assert resp.json()["results"][0]["reason"] == "new_key"
    row = _cache_row(db_session, START, "e2e4")
    assert row.source == "analysis"
    assert row.analysis_profile_id == BROWSER_ANALYSIS_MULTIPV_PROFILE_ID
    assert row.evidence_contract_id == "resolver-complete-v2"
    p = get_profile(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert row.engine_build == p.engine_build
    assert row.eval_file_id == p.eval_file_id
    assert row.move_san == "e4"  # server-derived
    assert row.played_eval == 30


def test_endpoint_replaces_browser_game_row(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _seed_browser_game_row(db_session, fen=START, move_uci="e2e4", played_eval=25, best_eval=25)
    resp = _post(client, auth_headers, session_id, [_evidence_row(played_eval=40, best_eval=40)])
    assert resp.json()["results"][0]["reason"] == "dominates_replace"
    row = _cache_row(db_session, START, "e2e4")
    assert row.source == "analysis"
    assert row.analysis_profile_id == BROWSER_ANALYSIS_MULTIPV_PROFILE_ID
    assert row.played_eval == 40


def test_endpoint_correctively_replaces_retired_analysis_row(client, auth_headers, create_game_session, db_session):
    # A stored RETIRED browser-analysis-v1 row (the defective hidden protocol) is
    # correctively replaced by the visible-MultiPV successor for the exact key.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _seed_browser_analysis_row(
        db_session, fen=START, move_uci="e2e4", played_eval=25, best_eval=25,
        profile=BROWSER_ANALYSIS_PROFILE_ID,
    )
    resp = _post(client, auth_headers, session_id, [_evidence_row(played_eval=40, best_eval=40)])
    result = resp.json()["results"][0]
    assert result["reason"] == "protocol_corrected_replace"
    row = _cache_row(db_session, START, "e2e4")
    assert row.analysis_profile_id == BROWSER_ANALYSIS_MULTIPV_PROFILE_ID
    assert row.played_eval == 40
    # An accepted write emits an upgrade for the open MoveList.
    assert result["upgrade"] is not None


def test_endpoint_stale_client_no_producer_fails_closed(client, auth_headers, create_game_session, db_session):
    # A stale client running the retired hidden worker sends no producer field ->
    # stale_producer, HTTP 200, nothing written.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    resp = _post(client, auth_headers, session_id, [_evidence_row()], producer=_NO_PRODUCER)
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["reason"] == "stale_producer"
    assert result["upgrade"] is None
    assert _cache_row(db_session, START, "e2e4") is None


def test_endpoint_unknown_producer_fails_closed(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    resp = _post(client, auth_headers, session_id, [_evidence_row()], producer="something-else-v9")
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["reason"] == "unknown_producer"
    assert _cache_row(db_session, START, "e2e4") is None


def test_endpoint_rederives_and_rejects_mislabeled_best(client, auth_headers, create_game_session, db_session):
    # Played e2e4 (in session) but the best move is d2d4 with a real drop, mislabeled
    # 'best' -> classification == best requires played UCI == best UCI -> rejected.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    row = _evidence_row(
        move_uci="e2e4", best_move_uci="d2d4", best_line_uci=("d2d4", "d7d5"),
        played_eval=-100, best_eval=100, eval_delta=200, classification="best",
    )
    resp = _post(client, auth_headers, session_id, [row])
    assert resp.json()["results"][0]["reason"] == "classification_mismatch"
    assert _cache_row(db_session, START, "e2e4") is None


def test_endpoint_rederives_and_rejects_mislabeled_lower_line(client, auth_headers, create_game_session, db_session):
    # A genuine large drop (best d2d4 far better) mislabeled as merely 'good'.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    row = _evidence_row(
        move_uci="e2e4", best_move_uci="d2d4", best_line_uci=("d2d4", "d7d5"),
        played_eval=-250, best_eval=120, eval_delta=370, classification="good",
    )
    resp = _post(client, auth_headers, session_id, [row])
    assert resp.json()["results"][0]["reason"] == "classification_mismatch"


def test_endpoint_accepts_correctly_labeled_lower_line(client, auth_headers, create_game_session, db_session):
    # A correctly-classified non-best move (small drop -> 'good') is accepted and
    # rederivation agrees.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    row = _evidence_row(
        move_uci="e2e4", best_move_uci="d2d4", best_line_uci=("d2d4", "d7d5"),
        played_eval=10, best_eval=40, eval_delta=30, classification="good",
    )
    resp = _post(client, auth_headers, session_id, [row])
    assert resp.json()["results"][0]["reason"] == "new_key"
    stored = _cache_row(db_session, START, "e2e4")
    assert stored is not None
    assert stored.classification == "good"


def test_endpoint_divergent_uci_inserts_new_key_and_leaves_old_row(client, auth_headers, create_game_session, db_session):
    # Defensive: an existing browser-game row recorded under a DIVERGENT uci for the
    # same real move. The analysis write lands on the derived-uci key (NEW_KEY) and
    # does not corrupt or replace the old browser-game row.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _seed_browser_game_row(db_session, fen=START, move_uci="g1f3", move_san="Nf3",
                           best_move_uci="g1f3", best_move_san="Nf3",
                           best_line_uci="g1f3 g8f6", played_eval=15, best_eval=15)
    resp = _post(client, auth_headers, session_id, [_evidence_row()])
    assert resp.json()["results"][0]["reason"] == "new_key"
    new_row = _cache_row(db_session, START, "e2e4")
    assert new_row.source == "analysis"
    old_row = _cache_row(db_session, START, "g1f3")
    assert old_row.source == "game"
    assert old_row.analysis_profile_id == BROWSER_PROFILE_ID
    assert old_row.played_eval == 15


# --------------------------------------------------------------------------- #
# endpoint: validation
# --------------------------------------------------------------------------- #
def test_endpoint_rejects_illegal_pv(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    row = _evidence_row(best_line_uci=("e2e4", "e2e4"))
    resp = _post(client, auth_headers, session_id, [row])
    assert resp.json()["results"][0]["reason"] == EVIDENCE_INVALID_LEGALITY
    assert _cache_row(db_session, START, "e2e4") is None


def test_endpoint_rejects_overlong_pv_422(client, auth_headers, create_game_session, db_session):
    # An over-cap best_line_uci 422s the whole batch at the wire, before any
    # per-ply legality work runs — the defensive bound against a shuffle line.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    row = _evidence_row(best_line_uci=tuple(["e2e4"] * 65))
    resp = _post(client, auth_headers, session_id, [row])
    assert resp.status_code == 422
    assert _cache_row(db_session, START, "e2e4") is None


def test_endpoint_writer_omits_survivor_surfaces_anomaly(
    client, auth_headers, create_game_session, db_session, monkeypatch
):
    # Contract backstop: if the shared writer ever returns but omits a survivor
    # key (violating its one-Reason-per-row contract), the row surfaces the
    # distinct writer_no_result reason instead of being masked as a false new_key.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    monkeypatch.setattr("app.api.session.write_analysis_cache_rows", lambda db, rows: [])
    resp = _post(client, auth_headers, session_id, [_evidence_row()])
    assert resp.json()["results"][0]["reason"] == EVIDENCE_WRITER_NO_RESULT


def test_endpoint_rejects_negative_delta(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    row = _evidence_row(played_eval=30, best_eval=30, eval_delta=-5)
    resp = _post(client, auth_headers, session_id, [row])
    assert resp.json()["results"][0]["reason"] == EVIDENCE_CONTRACT_UNSATISFIED


def test_endpoint_stores_white_cp_and_mate(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    # Mate stored both as a finite mate-to-cp eval AND raw mate count.
    row = _evidence_row(played_eval=31900, best_eval=31900, eval_delta=0,
                        played_eval_mate=1, best_eval_mate=1)
    resp = _post(client, auth_headers, session_id, [row])
    assert resp.json()["results"][0]["reason"] == "new_key"
    stored = _cache_row(db_session, START, "e2e4")
    assert stored.played_eval == 31900
    assert stored.played_eval_mate == 1
    assert stored.best_eval_mate == 1


# --------------------------------------------------------------------------- #
# endpoint: diagnostics
# --------------------------------------------------------------------------- #
def test_endpoint_response_order_and_survivor_writes(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _add_session_move(db_session, session_id, move_number=1, color="black",
                      move_san="e5", fen_before=FEN_AFTER_E4, fen_after=FEN_AFTER_E5)
    rows = [
        _evidence_row(),  # valid survivor for (START, e2e4)
        # A different in-session key with an illegal PV -> rejected, but does not
        # block the survivor written for the first key.
        _evidence_row(fen=FEN_AFTER_E4, move_uci="e7e5", best_move_uci="e7e5",
                      best_line_uci=("e7e5", "e7e5")),
        _evidence_row(),  # duplicate of the first (survivor) key
    ]
    resp = _post(client, auth_headers, session_id, rows)
    reasons = [r["reason"] for r in resp.json()["results"]]
    assert reasons == ["new_key", EVIDENCE_INVALID_LEGALITY, EVIDENCE_DUPLICATE_KEY]
    # The valid survivor still wrote despite a sibling rejection.
    assert _cache_row(db_session, START, "e2e4") is not None
    # The rejected sibling was never written.
    assert _cache_row(db_session, FEN_AFTER_E4, "e7e5") is None


def test_endpoint_rejected_primary_blocks_duplicate(client, auth_headers, create_game_session, db_session):
    # Dedup runs BEFORE legality (plan step 4 before step 5): the first occurrence is
    # the primary and is legality-checked; a later occurrence is always a duplicate,
    # even when the primary is rejected. Accepted defensive behavior — normal
    # operation submits single-row dwelled moves.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    rows = [
        _evidence_row(best_line_uci=("e2e4", "e2e4")),  # illegal PV primary
        _evidence_row(),  # valid, but same key -> duplicate
    ]
    resp = _post(client, auth_headers, session_id, rows)
    reasons = [r["reason"] for r in resp.json()["results"]]
    assert reasons == [EVIDENCE_INVALID_LEGALITY, EVIDENCE_DUPLICATE_KEY]
    assert _cache_row(db_session, START, "e2e4") is None


def test_endpoint_empty_rows(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    resp = _post(client, auth_headers, session_id, [])
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


# --------------------------------------------------------------------------- #
# session analysis wire fields
# --------------------------------------------------------------------------- #
def test_session_analysis_populates_wire_fields(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _add_session_move(db_session, session_id, move_number=1, color="white",
                      move_san="e4", fen_before=START, fen_after=FEN_AFTER_E4)
    _add_session_move(db_session, session_id, move_number=1, color="black",
                      move_san="e5", fen_before=FEN_AFTER_E4, fen_after=FEN_AFTER_E5)
    resp = client.get(f"/api/session/{session_id}/analysis", headers=auth_headers(user_id=123))
    assert resp.status_code == 200
    moves = resp.json()["moves"]
    by_key = {(m["move_number"], m["color"]): m for m in moves}
    assert by_key[(1, "white")]["fen_before"] == START
    assert by_key[(1, "white")]["move_uci"] == "e2e4"
    assert by_key[(1, "black")]["fen_before"] == FEN_AFTER_E4
    assert by_key[(1, "black")]["move_uci"] == "e7e5"


def test_session_analysis_wire_fields_null_for_legacy(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _add_session_move(db_session, session_id, move_number=1, color="white",
                      move_san="e4", fen_before=None, fen_after=FEN_AFTER_E4)
    resp = client.get(f"/api/session/{session_id}/analysis", headers=auth_headers(user_id=123))
    move = resp.json()["moves"][0]
    assert move["fen_before"] is None
    assert move["move_uci"] is None


# --------------------------------------------------------------------------- #
# Part B — endpoint returns the STORED row's MoveUpgrade on accepted writes
# --------------------------------------------------------------------------- #
def test_endpoint_new_key_returns_upgrade(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    resp = _post(client, auth_headers, session_id,
                 [_evidence_row(played_eval=45, best_eval=45, classification="best")])
    result = resp.json()["results"][0]
    assert result["reason"] == "new_key"
    up = result["upgrade"]
    assert up is not None
    assert up["classification"] == "best"
    # White to move -> mover-relative == white-relative (no flip). Non-authoritative.
    assert up["eval_cp"] == 45
    assert up["authoritative"] is False
    assert up["analysis_profile_id"] == BROWSER_ANALYSIS_MULTIPV_PROFILE_ID
    assert up["depth"] == 21


def test_endpoint_dominates_replace_returns_upgrade(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _seed_browser_game_row(db_session, fen=START, move_uci="e2e4", played_eval=25, best_eval=25)
    resp = _post(client, auth_headers, session_id,
                 [_evidence_row(played_eval=60, best_eval=60)])
    result = resp.json()["results"][0]
    assert result["reason"] == "dominates_replace"
    assert result["upgrade"] is not None
    assert result["upgrade"]["eval_cp"] == 60


def test_endpoint_idempotent_reads_stored_not_submitted_row(client, auth_headers, create_game_session, db_session):
    # Same-profile idempotent MERGE proof: the stored row carries a played_eval_mate
    # the RESUBMITTED row omits. The endpoint must read the STORED row back, so the
    # emitted upgrade reflects the stored mate (1), not the submitted None.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _seed_browser_analysis_row(
        db_session, fen=START, move_uci="e2e4",
        played_eval=31900, best_eval=31900, eval_delta=0, classification="best",
        played_eval_mate=1, best_eval_mate=1,
    )
    # Resubmit the SAME key WITHOUT the mate fields -> idempotent keep of the stored row.
    resp = _post(client, auth_headers, session_id,
                 [_evidence_row(played_eval=31900, best_eval=31900, eval_delta=0,
                                classification="best")])
    result = resp.json()["results"][0]
    assert result["reason"] == "same_profile_idempotent"
    up = result["upgrade"]
    assert up is not None
    assert up["eval_mate"] == 1  # from the STORED row, not the submitted (None)


def test_endpoint_incompatible_keep_no_upgrade(client, auth_headers, create_game_session, db_session):
    # A canonical row already holds the key; browser-analysis does not dominate
    # canonical -> incompatible_keep, a NOT-accepted verdict -> no upgrade.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    _seed_canonical_row(db_session, fen=START, move_uci="e2e4", played_eval=20, best_eval=20)
    resp = _post(client, auth_headers, session_id, [_evidence_row(played_eval=60, best_eval=60)])
    result = resp.json()["results"][0]
    assert result["reason"] == "incompatible_keep"
    assert result["upgrade"] is None


def test_endpoint_duplicate_row_does_not_inherit_primary_upgrade(
    client, auth_headers, create_game_session, db_session
):
    # A duplicate_request_key row shares the accepted primary's (fen, move_uci) but
    # never reached the writer -> it must return upgrade = None even though the
    # primary (new_key) carries one.
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    resp = _post(client, auth_headers, session_id, [_evidence_row(), _evidence_row()])
    results = resp.json()["results"]
    assert results[0]["reason"] == "new_key"
    assert results[0]["upgrade"] is not None
    assert results[1]["reason"] == EVIDENCE_DUPLICATE_KEY
    assert results[1]["upgrade"] is None


def test_endpoint_rejected_reasons_have_no_upgrade(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _seed_e4_move(db_session, session_id)
    rows = [
        _evidence_row(fen=FEN_AFTER_E4, move_uci="e7e5", best_move_uci="e7e5",
                      best_line_uci=("e7e5", "g1f3")),  # not_in_session
        _evidence_row(played_eval=None),  # contract_unsatisfied
    ]
    resp = _post(client, auth_headers, session_id, rows)
    results = resp.json()["results"]
    assert results[0]["reason"] == EVIDENCE_NOT_IN_SESSION
    assert results[0]["upgrade"] is None
    assert results[1]["reason"] == EVIDENCE_CONTRACT_UNSATISFIED
    assert results[1]["upgrade"] is None


def test_every_replacement_verdict_a_browser_row_can_earn_emits_an_upgrade():
    """D9: if the writer replaced the stored row, the open MoveList must be told.

    Membership, not endpoint behavior, because the omission fails SILENTLY: the
    write succeeds and the upgrade is simply never emitted, so no request errors and
    nothing shows up in the results payload. `strength_replace` was missing until the
    g-mk1d review — today's producer stamps a FIXED profile that meets every other
    profile across an edge or the authority barrier before the measured steps run,
    but that is a property of the current registry, not of this endpoint.
    """
    from app.analysis_cache_policy import Reason

    earnable = {
        Reason.DOMINATES_REPLACE,
        Reason.PROTOCOL_CORRECTED_REPLACE,
        Reason.STRENGTH_REPLACE,
    }
    assert {r.value for r in earnable} <= _EVIDENCE_ACCEPTED_REASONS
    # ...but authority reclamation is NOT earnable by a non-authoritative profile,
    # so accepting it would mask a writer regression rather than catch one.
    assert Reason.LEGACY_REPLACED_BY_AUTH.value not in _EVIDENCE_ACCEPTED_REASONS


# --------------------------------------------------------------------------- #
# Part C — read-time overlay in GET /session/{id}/analysis
# --------------------------------------------------------------------------- #
def test_session_analysis_overlays_eligible_upgrade(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    # Original game-time evidence graded e4 merely 'excellent'.
    _add_session_move(db_session, session_id, move_number=1, color="white",
                      move_san="e4", fen_before=START, fen_after=FEN_AFTER_E4,
                      classification="excellent")
    _seed_browser_analysis_row(db_session, fen=START, move_uci="e2e4",
                               classification="best", played_eval=45, best_eval=45)
    resp = client.get(f"/api/session/{session_id}/analysis", headers=auth_headers(user_id=123))
    move = resp.json()["moves"][0]
    # Base field stays on ORIGINAL evidence (aggregates read it); upgrade is alongside.
    assert move["classification"] == "excellent"
    assert move["upgraded"] is not None
    assert move["upgraded"]["classification"] == "best"
    assert move["upgraded"]["eval_cp"] == 45
    assert move["upgraded"]["authoritative"] is False


def test_session_analysis_no_upgrade_for_browser_game_only(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _add_session_move(db_session, session_id, move_number=1, color="white",
                      move_san="e4", fen_before=START, fen_after=FEN_AFTER_E4,
                      classification="excellent")
    # Only a browser-game row exists -> it does not dominate itself -> no overlay.
    _seed_browser_game_row(db_session, fen=START, move_uci="e2e4")
    resp = client.get(f"/api/session/{session_id}/analysis", headers=auth_headers(user_id=123))
    move = resp.json()["moves"][0]
    assert move["upgraded"] is None


def test_session_analysis_batch_overlays_multiple_moves(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123)
    _add_session_move(db_session, session_id, move_number=1, color="white",
                      move_san="e4", fen_before=START, fen_after=FEN_AFTER_E4)
    _add_session_move(db_session, session_id, move_number=1, color="black",
                      move_san="e5", fen_before=FEN_AFTER_E4, fen_after=FEN_AFTER_E5)
    _seed_browser_analysis_row(db_session, fen=START, move_uci="e2e4",
                               classification="best")
    _seed_browser_analysis_row(db_session, fen=FEN_AFTER_E4, move_uci="e7e5",
                               move_san="e5", best_move_uci="e7e5", best_move_san="e5",
                               best_line_uci="e7e5 g1f3", classification="best")
    resp = client.get(f"/api/session/{session_id}/analysis", headers=auth_headers(user_id=123))
    by_key = {(m["move_number"], m["color"]): m for m in resp.json()["moves"]}
    assert by_key[(1, "white")]["upgraded"] is not None
    assert by_key[(1, "black")]["upgraded"] is not None
    # Black to move at FEN_AFTER_E4 -> mover-relative eval flips vs white-relative 40.
    assert by_key[(1, "black")]["upgraded"]["eval_cp"] == -40


def test_session_analysis_null_move_uci_skipped(client, auth_headers, create_game_session, db_session):
    # A legacy move with null fen_before yields no join key and must not crash.
    session_id = create_game_session(user_id=123)
    _add_session_move(db_session, session_id, move_number=1, color="white",
                      move_san="e4", fen_before=None, fen_after=FEN_AFTER_E4)
    resp = client.get(f"/api/session/{session_id}/analysis", headers=auth_headers(user_id=123))
    assert resp.status_code == 200
    assert resp.json()["moves"][0]["upgraded"] is None
