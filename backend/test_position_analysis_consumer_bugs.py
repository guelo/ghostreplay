"""Red-baseline tests pinning the live non-trust-gated analysis consumers.

These document the g-position-analysis bug: consumers read engine evidence from
``analysis_cache`` (and session-move seeds) without a trust gate, so an untrusted
browser/legacy row can drive the result. Phase 1 only builds the storage + contract
primitives; the read-path cutover is Phase 4. So the consumer-bug tests are marked
``xfail(strict=True)``: they FAIL today (CI/pre-push stays green via expected-fail),
and when a later phase fixes the consumer they xpass — which strict mode turns into a
hard failure, forcing the implementer to drop the marker and assert for real.

Authoring rules that keep these xfailing TODAY (not xpassing/erroring):
- References to not-yet-existing API live INSIDE function bodies, never at import,
  so an AttributeError xfails rather than breaking collection.
- DIRECT attribute access for future fields (never ``getattr(obj, f, default)``):
  the default would make the assert pass today -> XPASS -> strict-fail tripwire.
- Per "trust gate != correctness": every xfail asserts BOTH that the untrusted value
  did NOT drive the result AND the positive correct/unavailable outcome — never
  merely ``result != browser_value``.

Trusted rows seed FULL canonical identity (authoritative profile + every
IDENTITY_FIELDS column + resolver-complete-v2) so the Phase-4 ``_is_authoritative``
trust gate accepts them; untrusted rows use browser-game-v1 + resolver-complete-v1.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile
from app.api.analysis import _trust_flags
from app.api.session import PositionAnalysis
from app.fen import normalize_fen
from app.models import AnalysisCache, Base
from app.tree_eval import MoveEval, lookup_move_evals, lookup_root_eval

# Real position with the documented mixed-source siblings from g-ul4p.
GUL4P_FEN = "rnbqkbnr/pp2pppp/2p5/3p4/3P4/5N2/PPP1PPPP/RNBQKB1R w KQkq - 0 3"
LINUX_PROFILE_ID = "canonical-sf18-depth24-linux-v1"

# Same position reached via different move orders => identical normalized FEN,
# different half/fullmove clocks (so requests resolve via the normalized fallback).
POS_A = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
POS_B = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 4 5"
POS_C = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7"

STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

CANON_CP = 18
BROWSER_CP = 77
CANON_BEST = 35
BROWSER_BEST = 30


# --- Seed helpers --------------------------------------------------------------


def _trusted_canonical_values(profile_id: str = CANONICAL_PROFILE_ID) -> dict:
    """Full canonical identity so the Phase-4 trust gate (_is_authoritative +
    resolver-complete-v2) accepts the row. Mirrors test_analysis_cache_api's
    ``_canonical_v2_seed_values``."""
    profile = get_profile(profile_id)
    values = {
        "source": "precomputed",
        "analysis_profile_id": profile_id,
        "evidence_contract_id": "resolver-complete-v2",
    }
    for field in IDENTITY_FIELDS:
        values[field] = getattr(profile, field)
    return values


def _untrusted_browser_values() -> dict:
    """A non-authoritative browser upload: identity will not match any canonical
    profile, so the Phase-4 trust gate must reject it."""
    return {
        "source": "game",
        "analysis_profile_id": "browser-game-v1",
        "evidence_contract_id": "resolver-complete-v1",
    }


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'consumer_bugs.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    yield s
    s.close()
    engine.dispose()


def _seed(session, *, fen, uci, **fields):
    """Insert a cache row, computing normalized_fen_before like the real writer."""
    row = AnalysisCache(
        fen_before=fen,
        normalized_fen_before=normalize_fen(fen),
        move_uci=uci,
        move_san=fields.pop("move_san", uci),
        **fields,
    )
    session.add(row)
    session.commit()
    return row


# --- 1. lookup_move_evals: untrusted normalized-fallback read (tree_eval.py) ----


@pytest.mark.xfail(
    reason="g-position-analysis Phase 4 cutover — lookup_move_evals' normalized "
    "fallback has no trust filter, so an untrusted browser eval currently drives it",
    strict=True,
)
def test_lookup_move_evals_untrusted_normalized_fallback(session):
    # ONLY an untrusted browser row supplies a played_eval, stored under a
    # clock-variant FEN so the request resolves via the normalized fallback. No
    # trusted canonical eval exists for this position+move.
    _seed(session, fen=POS_B, uci="f1c4", played_eval=BROWSER_CP, **_untrusted_browser_values())

    result = lookup_move_evals(session, [(POS_A, "f1c4")])[(POS_A, "f1c4")]

    # (a) the untrusted browser eval must NOT drive the result, AND
    # (b) with no trusted eval available, the correct outcome is "unavailable".
    assert result != MoveEval(cp=BROWSER_CP, mate=None)
    assert result is None


def test_lookup_move_evals_trusted_canonical_still_resolves(session):
    # Green companion (passes now AND after cutover): a FULL-identity trusted
    # canonical row and an untrusted browser row both transpose to the requested
    # position+move (distinct clock variants). The canonical eval must win — today
    # via source rank, after cutover via the trust gate. Guards against
    # over-correction: the Phase-4 fix must still return the canonical eval, not None.
    #
    # Both rows carry FULL contract-valid evidence so the difference between them is
    # purely trust, not completeness: the canonical row is resolver-complete-v2 +
    # authoritative (so the Phase-4 trust gate accepts it); the browser row is a
    # contract-valid resolver-complete-v1 row that is merely non-authoritative (so
    # the gate must reject it on TRUST, not on missing fields). POS_B is white to
    # move and f1c4 is its own best move, so eval_delta is 0.
    canonical = _seed(
        session,
        fen=POS_B,
        uci="f1c4",
        move_san="Bc4",
        best_move_uci="f1c4",
        best_move_san="Bc4",
        best_line_uci="f1c4 g8f6",
        played_eval=CANON_CP,
        best_eval=CANON_CP,
        eval_delta=0,
        classification="best",
        **_trusted_canonical_values(),
    )
    browser = _seed(
        session,
        fen=POS_C,
        uci="f1c4",
        move_san="Bc4",
        best_move_uci="f1c4",
        best_move_san="Bc4",
        best_line_uci="f1c4 g8f6",
        played_eval=BROWSER_CP,
        best_eval=BROWSER_CP,
        eval_delta=0,
        classification="good",
        **_untrusted_browser_values(),
    )

    # The seed is the point of this companion: only TRUST distinguishes the rows.
    # Both are contract-valid, but only the canonical row is trusted-for-resolution,
    # so the Phase-4 gate keeps the canonical eval rather than returning None.
    assert _trust_flags(canonical)[2] is True
    assert _trust_flags(browser)[2] is False

    result = lookup_move_evals(session, [(POS_A, "f1c4")])[(POS_A, "f1c4")]

    assert result == MoveEval(cp=CANON_CP, mate=None)
    assert result != MoveEval(cp=BROWSER_CP, mate=None)


# --- 2. g-ul4p mixed canonical/browser sibling case (tree_eval root ranking) ----


@pytest.mark.xfail(
    reason="g-position-analysis Phase 4 cutover — _root_sort_key has no "
    "authoritative/contract/trust filter, so the untrusted browser sibling can "
    "surface as the root eval",
    strict=True,
)
def test_root_eval_gul4p_untrusted_sibling_does_not_surface(session):
    # Trusted canonical c1f4 row: the engine's best move here is c2c4 (so this row's
    # move != its best_move). Full linux-canonical identity + resolver-complete-v2.
    _seed(
        session,
        fen=GUL4P_FEN,
        uci="c1f4",
        move_san="Bf4",
        best_move_uci="c2c4",
        best_move_san="c4",
        best_line_uci="c2c4 d5c4",
        best_eval=CANON_BEST,
        played_eval=44,
        eval_delta=0,
        classification="best",
        **_trusted_canonical_values(LINUX_PROFILE_ID),
    )
    # Untrusted browser c2c4 row that rates its OWN played move best (move ==
    # best_move). The documented browser row claims c1f4 best; we set it to c2c4 so
    # today's source-blind _root_sort_key ranks it ABOVE the trusted sibling via the
    # "complete best-move row" criterion (which precedes source rank). This pins the
    # MISSING trust filter directly: source rank merely happens to favor the
    # canonical row in the literal data, and "right answer via source rank" is not a
    # trust gate (trust gate != correctness).
    _seed(
        session,
        fen=GUL4P_FEN,
        uci="c2c4",
        move_san="c4",
        best_move_uci="c2c4",
        best_move_san="c4",
        best_line_uci="c2c4 d5c4",
        best_eval=BROWSER_BEST,
        played_eval=42,
        eval_delta=0,
        classification="best",
        **_untrusted_browser_values(),
    )

    result = lookup_root_eval(session, GUL4P_FEN)

    # (a) the untrusted browser best_eval must NOT drive the root, AND
    # (b) the trusted canonical best_eval is the correct root eval.
    assert result != MoveEval(cp=BROWSER_BEST, mate=None)
    assert result == MoveEval(cp=CANON_BEST, mate=None)


# --- 3. session.py position_analysis export seed --------------------------------


def _seed_untrusted_session_move(client, auth_headers, session_id, user_id):
    """Browser upload -> a session_move carrying fen_before + best_move_uci AND a
    browser-game-v1 (untrusted) analysis_cache row. This is the untrusted/legacy
    best-move seed the position_analysis export currently surfaces with no trust
    signal."""
    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": AFTER_E4_FEN,
                    "eval_cp": 20,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 20,
                    "eval_delta": 0,
                    "classification": "best",
                    "fen_before": STARTING_FEN,
                    "move_uci": "e2e4",
                    "best_move_uci": "e2e4",
                    "best_line_uci": ["e2e4", "e7e5"],
                }
            ]
        },
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200


def _position_entry(client, auth_headers, session_id, user_id):
    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200
    return response.json()["position_analysis"][STARTING_FEN]


@pytest.mark.xfail(
    reason="g-position-analysis Phase 4 cutover — the position_analysis export "
    "surfaces an untrusted/legacy best-move seed with no trust signal; Phase 4 must "
    "mark such seeds position_trusted=False",
    strict=True,
)
def test_session_position_analysis_untrusted_seed_marked_untrusted(
    client, auth_headers, create_game_session
):
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    _seed_untrusted_session_move(client, auth_headers, session_id, user_id)

    entry = PositionAnalysis.model_validate(
        _position_entry(client, auth_headers, session_id, user_id)
    )

    # (a) the untrusted seed currently drives the exported best move, AND
    assert entry.best_move_uci == "e2e4"
    # (b) the correct outcome is an explicit untrusted marker. DIRECT attribute
    # access: today PositionAnalysis (session.py) has no position_trusted field, so
    # this raises AttributeError -> xfailed. After Phase 4 marks the seed it reads
    # False -> passes -> xpass -> strict-fail tripwire.
    assert entry.position_trusted is False


def test_session_position_analysis_current_shape(
    client, auth_headers, create_game_session
):
    # Green characterization (current shape). When Phase 4 adds position_trusted this
    # MUST be updated — that is the point: it shows Phase 4 exactly what changes.
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    _seed_untrusted_session_move(client, auth_headers, session_id, user_id)

    entry = _position_entry(client, auth_headers, session_id, user_id)

    assert entry["best_move_uci"] == "e2e4"  # raw seed, exported verbatim
    assert "position_trusted" not in entry  # no trust signal today
