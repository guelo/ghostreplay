"""End-to-end per-row browser provenance through POST /moves (g-mk1d §2).

The invariant under test is per-ROW degradation: one bad provenance claim must
cost exactly one cache row and nothing else. It must never 422 the batch, never
suppress a sibling move, never block the player's own eval/classification from
persisting, and never be laundered into a silent legacy downgrade.
"""

import json
import logging
import uuid

import pytest

from app.analysis_profiles import BROWSER_GAME_V2_PROFILE_ID, BROWSER_PROFILE_ID
from app.models import AnalysisCache, SessionMove

STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
FEN_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

BUILD = "a8fbc05ec6920b56d7485826dcb02c5ffd2826bcbf751cf973046f237a9096f1"
NET = (
    "nn-9067e33176e8.nnue:"
    "9067e33176e8c5edb7aa8db6a3aedd012f84a1f39872e86357c6c2d0993f314d"
)


def provenance(depth=17, **overrides):
    return {
        "engine_version": "18",
        "engine_build": BUILD,
        "eval_file_id": NET,
        "search_limit_type": "depth",
        "search_limit_value": depth,
        "threads": 1,
        "hash_mb": 128,
        **overrides,
    }


def move(color="white", *, fen_before=STARTING_FEN, uci="e2e4", san="e4", **overrides):
    payload = {
        "move_number": 1,
        "color": color,
        "move_san": san,
        "fen_before": fen_before,
        "fen_after": FEN_AFTER_E4,
        "move_uci": uci,
        "eval_cp": 20,
        "best_move_san": san,
        "best_move_eval_cp": 20,
        "eval_delta": 0,
        "classification": "best",
    }
    payload.update(overrides)
    return payload


def post_moves(client, auth_headers, session_id, moves, user_id=123):
    return client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": moves},
        headers=auth_headers(user_id=user_id),
    )


def cache_row(db_session, uci="e2e4", fen_before=STARTING_FEN):
    return (
        db_session.query(AnalysisCache)
        .filter(
            AnalysisCache.fen_before == fen_before,
            AnalysisCache.move_uci == uci,
        )
        .one_or_none()
    )


# --- stamping ------------------------------------------------------------------


def test_valid_provenance_stamps_browser_game_v2_with_dynamic_columns(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")
    response = post_moves(
        client, auth_headers, session_id, [move(provenance=provenance(20))]
    )
    assert response.status_code == 200

    row = cache_row(db_session)
    assert row.analysis_profile_id == BROWSER_GAME_V2_PROFILE_ID
    assert row.search_limit_type == "depth"
    assert row.search_limit_value == 20
    assert row.engine_build == BUILD
    assert row.eval_file_id == NET
    assert row.threads == 1
    assert row.hash_mb == 128
    # The FIXED half is server-stamped from the registry, never from the wire.
    assert row.engine_name == "Stockfish"
    assert row.multipv == 1
    assert row.analyzer_protocol_version == "browser-analyzer-v1"


def test_absent_provenance_keeps_the_legacy_v1_stamp(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")
    assert post_moves(client, auth_headers, session_id, [move()]).status_code == 200

    row = cache_row(db_session)
    assert row.analysis_profile_id == BROWSER_PROFILE_ID
    assert row.search_limit_value is None
    assert row.engine_build is None


def test_malformed_provenance_drops_only_that_row_from_the_cache(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")
    response = post_moves(
        client,
        auth_headers,
        session_id,
        [
            move(provenance=provenance(17, search_limit_value=0)),
            move(
                color="black",
                fen_before=FEN_AFTER_E4,
                uci="e7e5",
                san="e5",
                provenance=provenance(17),
            ),
        ],
    )
    assert response.status_code == 200
    assert response.json()["moves_inserted"] == 2

    # The malformed row contributes NO cache evidence — not even a silently
    # downgraded v1 row, which would launder a bad claim into a good-looking one.
    assert cache_row(db_session, "e2e4") is None
    # ...while its sibling is stamped normally.
    assert cache_row(db_session, "e7e5", FEN_AFTER_E4).analysis_profile_id == (
        BROWSER_GAME_V2_PROFILE_ID
    )

    # The player's own move still persists and still displays.
    rows = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(session_id))
        .all()
    )
    assert len(rows) == 2
    malformed_row = next(r for r in rows if r.color == "white")
    assert malformed_row.classification == "best"
    assert malformed_row.eval_cp == 20
    assert malformed_row.browser_provenance is None


# --- the fully permissive wire shape -------------------------------------------


@pytest.mark.parametrize(
    "bad", [[], "depth", 17, 1.5, True, [{"engine_version": "18"}], {"nope": 1}]
)
def test_no_provenance_shape_can_422_the_batch(
    client, auth_headers, create_game_session, db_session, bad
):
    # The wire field is `Any` precisely so a malformed claim degrades per row. A
    # constrained Pydantic shape would reject non-objects during request parsing
    # and fail the WHOLE upload before the endpoint body ever ran.
    session_id = create_game_session(user_id=123, player_color="white")
    response = post_moves(
        client,
        auth_headers,
        session_id,
        [
            move(provenance=bad),
            move(
                color="black",
                fen_before=FEN_AFTER_E4,
                uci="e7e5",
                san="e5",
                provenance=provenance(17),
            ),
        ],
    )
    assert response.status_code == 200
    assert response.json()["moves_inserted"] == 2
    # The good sibling row is written regardless of the bad one's shape.
    assert cache_row(db_session, "e7e5", FEN_AFTER_E4) is not None


def test_a_constrained_dict_wire_shape_would_have_422d(monkeypatch):
    # Regression anchor for WHY the field is `Any`: the same non-object payload
    # under a `dict[str, object] | None` annotation fails schema validation, which
    # in the endpoint would have 422'd the entire batch.
    from pydantic import BaseModel, ValidationError

    class Constrained(BaseModel):
        provenance: dict[str, object] | None = None

    with pytest.raises(ValidationError):
        Constrained(provenance=[1, 2, 3])
    # ...whereas the shipped model accepts it and defers to the row validator.
    from app.api.session import SessionMoveInput

    parsed = SessionMoveInput(
        move_number=1,
        color="white",
        move_san="e4",
        fen_after=FEN_AFTER_E4,
        provenance=[1, 2, 3],
    )
    assert parsed.provenance == [1, 2, 3]


# --- observability -------------------------------------------------------------


def test_summary_log_distinguishes_valid_absent_and_malformed(
    client, auth_headers, create_game_session, caplog
):
    session_id = create_game_session(user_id=123, player_color="white")
    with caplog.at_level(logging.INFO):
        post_moves(
            client,
            auth_headers,
            session_id,
            [
                move(provenance=provenance(17)),
                move(color="black", fen_before=FEN_AFTER_E4, uci="e7e5", san="e5"),
                move(
                    move_number=2,
                    fen_before="fen-3",
                    uci="g1f3",
                    san="Nf3",
                    provenance=provenance(17, threads=99),
                ),
            ],
        )

    line = next(
        r.getMessage()
        for r in caplog.records
        if "side_effect=analysis_cache_write" in r.getMessage()
    )
    assert "provenance_valid=1" in line
    assert "provenance_absent=1" in line
    assert "provenance_malformed=1" in line
    # Malformed dominates the per-run verdict, so a client bug stays visible.
    assert "session_provenance=mixed_malformed" in line


@pytest.mark.parametrize(
    "moves_provenance,expected",
    [([provenance(17)], "v2"), ([None], "legacy")],
)
def test_per_run_verdict_is_length_independent(
    client, auth_headers, create_game_session, caplog, moves_provenance, expected
):
    session_id = create_game_session(user_id=123, player_color="white")
    payload = [move(provenance=p) if p else move() for p in moves_provenance]
    with caplog.at_level(logging.INFO):
        post_moves(client, auth_headers, session_id, payload)

    line = next(
        r.getMessage()
        for r in caplog.records
        if "side_effect=analysis_cache_write" in r.getMessage()
    )
    assert f"session_provenance={expected}" in line


# --- session_moves persistence -------------------------------------------------


def test_valid_provenance_persists_on_the_session_move(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")
    post_moves(client, auth_headers, session_id, [move(provenance=provenance(19))])

    row = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(session_id))
        .one()
    )
    assert json.loads(row.browser_provenance) == provenance(19)


@pytest.mark.parametrize(
    "value", [None, {"engine_build": "nope"}, [1, 2, 3], "depth"]
)
def test_absent_or_malformed_provenance_persists_null(
    client, auth_headers, create_game_session, db_session, value
):
    # A stored operand must be well-formed or absent; never a bad claim that could
    # later masquerade as a comparison operand.
    session_id = create_game_session(user_id=123, player_color="white")
    payload = move() if value is None else move(provenance=value)
    post_moves(client, auth_headers, session_id, [payload])

    row = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(session_id))
        .one()
    )
    assert row.browser_provenance is None


def test_reupload_overwrites_the_persisted_provenance(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")
    post_moves(client, auth_headers, session_id, [move(provenance=provenance(17))])
    post_moves(client, auth_headers, session_id, [move(provenance=provenance(20))])

    row = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(session_id))
        .one()
    )
    assert json.loads(row.browser_provenance)["search_limit_value"] == 20


def test_synthetic_terminal_rows_carry_no_provenance_and_no_cache_row(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")
    post_moves(
        client,
        auth_headers,
        session_id,
        [move(synthetic_terminal_eval=True, provenance=provenance(17))],
    )
    assert cache_row(db_session) is None
    row = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(session_id))
        .one()
    )
    # The flag itself is transient, but a never-searched row must not claim a
    # search. This is the load-bearing assertion: the cache correctly refuses the
    # row, and a persisted tuple here would smuggle the same fabricated eval back
    # in as browser_live_descriptor's "live search" overlay operand.
    assert row.browser_provenance is None
    assert row.eval_cp == 20


# --- cross-session replacement -------------------------------------------------


def test_a_deeper_later_upload_replaces_a_shallower_stored_row(
    client, auth_headers, create_game_session, db_session
):
    first = create_game_session(user_id=123, player_color="white")
    post_moves(client, auth_headers, first, [move(provenance=provenance(17))])

    second = create_game_session(user_id=456, player_color="white")
    post_moves(
        client, auth_headers, second, [move(provenance=provenance(20))], user_id=456
    )

    db_session.expire_all()
    assert cache_row(db_session).search_limit_value == 20


def test_a_shallower_later_upload_does_not_discard_the_stronger_row(
    client, auth_headers, create_game_session, db_session
):
    first = create_game_session(user_id=123, player_color="white")
    post_moves(client, auth_headers, first, [move(provenance=provenance(20))])

    second = create_game_session(user_id=456, player_color="white")
    post_moves(
        client, auth_headers, second, [move(provenance=provenance(17))], user_id=456
    )

    db_session.expire_all()
    assert cache_row(db_session).search_limit_value == 20


def test_a_dynamic_upload_does_not_reclaim_a_legacy_row(
    client, auth_headers, create_game_session, db_session
):
    # An all-None v1 row is UNKNOWN strength, not weak: depth alone must not
    # replace it. (This is exactly why the v1 write-retirement cutover exists.)
    first = create_game_session(user_id=123, player_color="white")
    post_moves(client, auth_headers, first, [move()])

    second = create_game_session(user_id=456, player_color="white")
    post_moves(
        client, auth_headers, second, [move(provenance=provenance(20))], user_id=456
    )

    db_session.expire_all()
    row = cache_row(db_session)
    assert row.analysis_profile_id == BROWSER_PROFILE_ID
    assert row.search_limit_value is None


# --- Part C: the refetch overlay -----------------------------------------------


def _analysis(client, auth_headers, session_id, user_id=123):
    response = client.get(
        f"/api/session/{session_id}/analysis", headers=auth_headers(user_id=user_id)
    )
    assert response.status_code == 200
    return response.json()


def _seed_cross_user_row(client, auth_headers, create_game_session, depth):
    """Another user stores a browser-game-v2 row for the same key at ``depth``."""
    other = create_game_session(user_id=456, player_color="white")
    post_moves(
        client, auth_headers, other, [move(provenance=provenance(depth))], user_id=456
    )


@pytest.mark.parametrize(
    "stored_depth,own_depth,overlaid",
    [
        (20, 17, True),   # strictly stronger cross-user row wins
        (17, 17, False),  # equal strength is not an upgrade
        (17, 20, False),  # weaker than what this session already searched
    ],
)
def test_overlay_requires_the_stored_row_to_beat_this_sessions_own_search(
    client, auth_headers, create_game_session, stored_depth, own_depth, overlaid
):
    mine = create_game_session(user_id=123, player_color="white")
    post_moves(client, auth_headers, mine, [move(provenance=provenance(own_depth))])
    _seed_cross_user_row(client, auth_headers, create_game_session, stored_depth)

    upgraded = _analysis(client, auth_headers, mine)["moves"][0]["upgraded"]
    assert (upgraded is not None) is overlaid
    if overlaid:
        assert upgraded["depth"] == stored_depth
        # Non-authoritative: a browser diagnostic never claims position authority.
        assert upgraded["authoritative"] is False


def test_a_legacy_session_without_provenance_keeps_its_own_label(
    client, auth_headers, create_game_session
):
    # No live operand -> incomparable -> no overlay. Safe by construction: GET
    # already renders the player's own uploaded classification.
    mine = create_game_session(user_id=123, player_color="white")
    post_moves(client, auth_headers, mine, [move()])
    _seed_cross_user_row(client, auth_headers, create_game_session, 20)

    entry = _analysis(client, auth_headers, mine)["moves"][0]
    assert entry["upgraded"] is None
    assert entry["classification"] == "best"


def test_a_tampered_stored_operand_withholds_the_overlay(
    client, auth_headers, create_game_session, db_session
):
    mine = create_game_session(user_id=123, player_color="white")
    post_moves(client, auth_headers, mine, [move(provenance=provenance(17))])
    _seed_cross_user_row(client, auth_headers, create_game_session, 20)

    row = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(mine))
        .one()
    )
    row.browser_provenance = json.dumps({"engine_build": "tampered"})
    db_session.commit()

    assert _analysis(client, auth_headers, mine)["moves"][0]["upgraded"] is None


def test_a_synthetic_terminal_row_cannot_fabricate_a_live_overlay_operand(
    client, auth_headers, create_game_session
):
    """A fabricated terminal eval must not act as a search in the overlay compare.

    ``_upsert_analysis_cache`` already refuses to CACHE a synthetic-terminal row,
    because no search produced its eval. Persisting a provenance tuple for that
    same row would reintroduce it through the other door: GET /analysis reads
    ``session_moves.browser_provenance`` as the session's own live-search operand,
    so a depth-25 claim attached to a made-up eval would out-rank — and silently
    suppress — a genuine stored row that deserves to overlay.
    """
    mine = create_game_session(user_id=123, player_color="white")
    post_moves(
        client,
        auth_headers,
        mine,
        [move(synthetic_terminal_eval=True, provenance=provenance(25))],
    )
    _seed_cross_user_row(client, auth_headers, create_game_session, 20)

    entry = _analysis(client, auth_headers, mine)["moves"][0]
    # The claim is discarded, so there is no live operand at all -> incomparable
    # -> the overlay is withheld. Withholding is the safe direction; the point is
    # that the fabricated depth-25 tuple never became the thing being compared.
    assert entry["upgraded"] is None


# --- adoption finality ---------------------------------------------------------


CLIENT_ID = "7f3e4d2a-1b2c-4d5e-8f90-abcdef123456"


def _cache_write_line(caplog):
    return next(
        r.getMessage()
        for r in caplog.records
        if "side_effect=analysis_cache_write" in r.getMessage()
    )


@pytest.mark.parametrize(
    "body_extra,expected",
    [
        # The end-of-session final_full upload: terminal_action present.
        ({"terminal_action": "game_end"}, "session_final=True"),
        # A revert upload. It sets recompute_opportunity=True WITHOUT ending the
        # session — the exact case that makes run_opportunity unusable as the
        # adoption metric's finality signal.
        ({"recompute_opportunity": True}, "session_final=False"),
        # A pre-g-y90g client, which defaults recompute_opportunity True on every
        # mid-game upload. Same trap.
        ({}, "session_final=False"),
    ],
)
def test_session_final_tracks_terminal_action_not_run_opportunity(
    client, auth_headers, create_game_session, caplog, body_extra, expected
):
    session_id = create_game_session(user_id=123, player_color="white")
    headers = auth_headers(user_id=123)
    headers["X-Client-Request-ID"] = CLIENT_ID
    with caplog.at_level(logging.INFO):
        response = client.post(
            f"/api/session/{session_id}/moves",
            json={"moves": [move(provenance=provenance(17))], **body_extra},
            headers=headers,
        )
    assert response.status_code == 200, response.text

    line = _cache_write_line(caplog)
    assert expected in line
    # The g-dckw latency cohort is untouched: it still keys on run_opportunity, so
    # adding session_final did not silently re-cohort an existing metric. Match on
    # the leading space so this cannot be satisfied by `session_final=True`.
    assert " final=True" in line
    assert "kind=final" in line
