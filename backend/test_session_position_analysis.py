"""Phase 4 session drill-review export: trusted position evidence (g-position-analysis.4).

The ``position_analysis`` map is the WIRE grain — keyed by the original full
``fen_before`` — but it now sources best-move evidence from the trusted resolver
(``position_analysis`` storage winner, else a legacy resolver-complete-v2 projection)
sourced by NORMALIZED FEN, falling back to the untrusted ``SessionMove`` seed only
when no trusted position exists. ``position_trusted`` flags which path produced each
entry, and storage evals (white-relative) are sign-converted to side-to-move-relative.
"""

from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile
from app.fen import normalize_fen
from app.models import AnalysisCache, PositionAnalysisRow

STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
AFTER_E4E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


def _identity() -> dict:
    profile = get_profile(CANONICAL_PROFILE_ID)
    return {f: getattr(profile, f) for f in IDENTITY_FIELDS}


def _seed_storage(db, *, fen, best_move_uci, best_line_uci, best_eval=None, best_eval_mate=None):
    db.add(PositionAnalysisRow(
        normalized_fen=normalize_fen(fen), fen=fen,
        best_move_uci=best_move_uci, best_move_san=best_move_uci,
        best_line_uci=best_line_uci, best_eval=best_eval, best_eval_mate=best_eval_mate,
        source="precomputed", analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id="position-complete-v1", **_identity(),
    ))
    db.commit()


def _seed_legacy_v2(db, *, fen, move_uci, best_move_uci, best_line_uci, best_eval, played_eval):
    db.add(AnalysisCache(
        fen_before=fen, normalized_fen_before=normalize_fen(fen),
        move_uci=move_uci, move_san=move_uci,
        best_move_uci=best_move_uci, best_move_san=best_move_uci,
        best_line_uci=best_line_uci, best_eval=best_eval, played_eval=played_eval,
        eval_delta=0, classification="best", source="precomputed",
        analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id="resolver-complete-v2", **_identity(),
    ))
    db.commit()


def _upload_move(client, auth_headers, session_id, user_id, *, move_number, color,
                 move_san, fen_before, fen_after, move_uci, best_move_uci=None,
                 best_line_uci=None, eval_cp=20):
    payload = {
        "move_number": move_number, "color": color, "move_san": move_san,
        "fen_after": fen_after, "fen_before": fen_before, "move_uci": move_uci,
        "eval_cp": eval_cp,
    }
    if best_move_uci is not None:
        payload["best_move_uci"] = best_move_uci
        payload["best_move_san"] = best_move_uci
        payload["best_move_eval_cp"] = eval_cp
    if best_line_uci is not None:
        payload["best_line_uci"] = best_line_uci
    resp = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": [payload]},
        headers=auth_headers(user_id=user_id),
    )
    assert resp.status_code == 200


def _position_analysis(client, auth_headers, session_id, user_id) -> dict:
    resp = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=user_id),
    )
    assert resp.status_code == 200
    return resp.json()["position_analysis"]


def test_trusted_storage_drives_entry_keyed_by_full_fen(
    client, auth_headers, create_game_session, db_session
):
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    # Browser seed proposes e2e4; the trusted storage winner says d2d4 and must win.
    _upload_move(client, auth_headers, session_id, user_id,
                 move_number=1, color="white", move_san="e4",
                 fen_before=STARTING_FEN, fen_after=AFTER_E4_FEN,
                 move_uci="e2e4", best_move_uci="e2e4")
    _seed_storage(db_session, fen=STARTING_FEN, best_move_uci="d2d4",
                  best_line_uci="d2d4 d7d5", best_eval=50)

    pa = _position_analysis(client, auth_headers, session_id, user_id)
    # Keyed by the ORIGINAL full fen_before, not the normalized storage key.
    assert STARTING_FEN in pa
    entry = pa[STARTING_FEN]
    assert entry["position_trusted"] is True
    assert entry["best_move_uci"] == "d2d4"  # storage winner, not the e2e4 seed
    assert entry["best_line_uci"] == ["d2d4", "d7d5"]
    # White to move -> white-relative 50 stays +50 side-to-move-relative.
    assert entry["best_move_eval_cp"] == 50


def test_trusted_storage_eval_sign_converted_for_black_to_move(
    client, auth_headers, create_game_session, db_session
):
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    _upload_move(client, auth_headers, session_id, user_id,
                 move_number=1, color="white", move_san="e4",
                 fen_before=STARTING_FEN, fen_after=AFTER_E4_FEN,
                 move_uci="e2e4", best_move_uci="e2e4")
    _upload_move(client, auth_headers, session_id, user_id,
                 move_number=1, color="black", move_san="e5",
                 fen_before=AFTER_E4_FEN, fen_after=AFTER_E4E5_FEN,
                 move_uci="e7e5", best_move_uci="e7e5")
    _seed_storage(db_session, fen=STARTING_FEN, best_move_uci="d2d4",
                  best_line_uci="d2d4 d7d5", best_eval=50)
    _seed_storage(db_session, fen=AFTER_E4_FEN, best_move_uci="g8f6",
                  best_line_uci="g8f6 b1c3", best_eval=60)

    pa = _position_analysis(client, auth_headers, session_id, user_id)
    # White to move: +50 unchanged.
    assert pa[STARTING_FEN]["best_move_eval_cp"] == 50
    # Black to move: white-relative +60 -> side-to-move-relative -60.
    assert pa[AFTER_E4_FEN]["position_trusted"] is True
    assert pa[AFTER_E4_FEN]["best_move_eval_cp"] == -60


def test_trusted_storage_emitted_even_when_session_seed_has_no_best_move(
    client, auth_headers, create_game_session, db_session
):
    # Finding 2a: a move with fen_before but NO SessionMove.best_move_uci would have
    # produced no entry under the old gate; with a trusted storage winner it is now
    # emitted (position_trusted=True), sourced entirely from storage.
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    _upload_move(client, auth_headers, session_id, user_id,
                 move_number=1, color="white", move_san="e4",
                 fen_before=STARTING_FEN, fen_after=AFTER_E4_FEN,
                 move_uci="e2e4", best_move_uci=None)
    _seed_storage(db_session, fen=STARTING_FEN, best_move_uci="d2d4",
                  best_line_uci="d2d4 d7d5", best_eval=40)

    pa = _position_analysis(client, auth_headers, session_id, user_id)
    assert STARTING_FEN in pa
    assert pa[STARTING_FEN]["position_trusted"] is True
    assert pa[STARTING_FEN]["best_move_uci"] == "d2d4"


def test_trusted_legacy_v2_cache_fallback_when_no_storage_row(
    client, auth_headers, create_game_session, db_session
):
    # Finding 2b: no storage row, but a trusted resolver-complete-v2 analysis_cache
    # row at the normalized FEN -> entry emitted via the legacy projection.
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    _upload_move(client, auth_headers, session_id, user_id,
                 move_number=1, color="white", move_san="e4",
                 fen_before=STARTING_FEN, fen_after=AFTER_E4_FEN,
                 move_uci="e2e4", best_move_uci="e2e4")
    # Trusted v2 row at a DIFFERENT move key so it does not collide with the browser
    # upload's (STARTING_FEN, e2e4) row; it carries the position's true best move.
    _seed_legacy_v2(db_session, fen=STARTING_FEN, move_uci="d2d4",
                    best_move_uci="d2d4", best_line_uci="d2d4 d7d5",
                    best_eval=45, played_eval=45)

    pa = _position_analysis(client, auth_headers, session_id, user_id)
    assert pa[STARTING_FEN]["position_trusted"] is True
    assert pa[STARTING_FEN]["best_move_uci"] == "d2d4"
    assert pa[STARTING_FEN]["best_move_eval_cp"] == 45  # white to move
