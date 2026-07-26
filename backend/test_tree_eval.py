"""Tests for the opening-tree analysis_cache eval lookup helper (app/tree_eval.py)."""

import pytest
from sqlalchemy import create_engine, tuple_
from sqlalchemy.orm import sessionmaker

from app.analysis_cache_repo import write_analysis_cache_rows
from app.analysis_profiles import (
    BROWSER_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    JEFFML_PROFILE_ID,
    get_profile,
)
from app.evidence_contracts import MINIMAL_PLAYED_EVAL
from app.fen import normalize_fen
from app.models import AnalysisCache, Base, PositionAnalysisRow
from app.tree_eval import (
    MoveEval,
    eval_for_perspective,
    lookup_move_evals,
    lookup_root_eval,
)

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# Same position reached via two move orders => identical normalized FEN, different clocks.
POS_A = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
POS_B = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 4 5"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tree_eval.db'}")
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    s = Factory()
    yield s
    s.close()
    engine.dispose()


def _canon_identity(profile_id: str = CANONICAL_PROFILE_ID) -> dict:
    """Full canonical identity + the legacy resolver-complete-v2 contract, so a
    seeded row passes the Phase-4 move/position trust gates. Identity (not the
    ``source`` column) is what grants trust, so callers may still vary ``source``."""
    profile = get_profile(profile_id)
    values = {
        "analysis_profile_id": profile_id,
        "evidence_contract_id": "resolver-complete-v2",
    }
    for field in IDENTITY_FIELDS:
        values[field] = getattr(profile, field)
    return values


def _seed(session, *, fen, uci, source="game", trusted=True, **fields):
    """Insert a cache row, computing normalized_fen_before like the real writer.

    Rows are TRUSTED by default (full canonical identity + resolver-complete-v2 +
    a classification, so they clear both the move- and position-grain trust gates the
    Phase-4 lookups apply). Pass ``trusted=False`` for an untrusted no-identity row.
    """
    row_fields = dict(fields)
    move_san = row_fields.pop("move_san", uci)
    if trusted:
        row_fields.setdefault("classification", "best")
        for key, value in _canon_identity().items():
            row_fields.setdefault(key, value)
    row = AnalysisCache(
        fen_before=fen,
        normalized_fen_before=normalize_fen(fen),
        move_uci=uci,
        move_san=move_san,
        source=source,
        **row_fields,
    )
    session.add(row)
    session.commit()
    return row


# --- exact full-FEN hits -------------------------------------------------------

def test_exact_full_fen_hit_returns_cp(session):
    _seed(session, fen=POS_A, uci="f1c4", played_eval=42)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=42, mate=None)


def test_exact_hit_prefers_mate_over_cp(session):
    _seed(session, fen=POS_A, uci="f1c4", played_eval=42, played_eval_mate=3)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=None, mate=3)


# --- normalized transposition fallback ----------------------------------------

def test_normalized_fallback_on_clock_variant(session):
    # Cache row stored under POS_B; request the clock-variant POS_A (no exact row).
    _seed(session, fen=POS_B, uci="f1c4", played_eval=15)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=15, mate=None)


def test_fallback_prefers_mate_same_position(session):
    # Two rows, both normalize to POS_A's position, same move: cp vs mate.
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10)
    _seed(
        session,
        fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
        uci="f1c4",
        played_eval=20,
        played_eval_mate=2,
    )
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=None, mate=2)


def test_fallback_source_ordering_precomputed_over_game(session):
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10, source="game")
    _seed(
        session,
        fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
        uci="f1c4",
        played_eval=20,
        source="precomputed",
    )
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=20, mate=None)


def test_fallback_stable_id_tiebreak(session):
    first = _seed(session, fen=POS_B, uci="f1c4", played_eval=11, source="precomputed")
    _seed(
        session,
        fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
        uci="f1c4",
        played_eval=22,
        source="precomputed",
    )
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    # Same source, no mate -> lowest id wins.
    assert out[(POS_A, "f1c4")] == MoveEval(cp=11, mate=None)
    assert first.id == min(r.id for r in session.query(AnalysisCache).all())


def test_fallback_does_not_cross_moves(session):
    # Same position, different move with a richer eval must not leak into f1c4.
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10)
    _seed(session, fen=POS_B, uci="f1b5", played_eval=99)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=10, mate=None)


# --- root eval -----------------------------------------------------------------

def test_root_eval_from_non_best_move_row(session):
    # A played-move row whose move is NOT the engine best move still carries best_eval.
    _seed(
        session,
        fen=START,
        uci="e2e4",
        played_eval=18,
        best_move_uci="d2d4",
        best_line_uci="d2d4 g8f6",
        best_eval=30,
    )
    assert lookup_root_eval(session, START) == MoveEval(cp=30, mate=None)


def test_root_eval_prefers_complete_best_move_row(session):
    _seed(session, fen=START, uci="e2e4", best_move_uci="d2d4",
          best_line_uci="d2d4 d7d5", best_eval=25)
    _seed(session, fen=START, uci="d2d4", best_move_uci="d2d4",
          best_line_uci="d2d4 d7d5", best_eval=31)
    # Both have usable best_eval; the row where move==best_move is preferred.
    assert lookup_root_eval(session, START) == MoveEval(cp=31, mate=None)


def test_root_eval_mate(session):
    _seed(session, fen=START, uci="e2e4", best_move_uci="e2e4",
          best_line_uci="e2e4 e7e5", best_eval=20, best_eval_mate=5)
    assert lookup_root_eval(session, START) == MoveEval(cp=None, mate=5)


def test_root_eval_missing_returns_none(session):
    _seed(session, fen=START, uci="e2e4", played_eval=18)  # no best_eval
    assert lookup_root_eval(session, START) is None


# --- misses & no-usable-eval ---------------------------------------------------

def test_cache_miss_returns_none(session):
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] is None


def test_exact_without_usable_eval_falls_back(session):
    # Exact row exists but carries no played eval; a transposition does.
    _seed(session, fen=POS_A, uci="f1c4", best_eval=5)  # best_eval only, no played eval
    _seed(session, fen=POS_B, uci="f1c4", played_eval=12)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=12, mate=None)


def test_batch_mixes_exact_fallback_and_miss(session):
    _seed(session, fen=POS_A, uci="f1c4", played_eval=42)   # exact
    _seed(session, fen=POS_B, uci="f1b5", played_eval=7)    # fallback for POS_A/f1b5
    out = lookup_move_evals(session, [(POS_A, "f1c4"), (POS_A, "f1b5"), (POS_A, "g1f3")])
    assert out == {
        (POS_A, "f1c4"): MoveEval(cp=42, mate=None),
        (POS_A, "f1b5"): MoveEval(cp=7, mate=None),
        (POS_A, "g1f3"): None,
    }


def test_empty_requests(session):
    assert lookup_move_evals(session, []) == {}


# --- Phase 4: trust gate + position_analysis storage resolution ---------------


def _seed_position(
    session,
    *,
    fen,
    best_move_uci="f1c4",
    best_line_uci="f1c4 g8f6",
    best_eval=None,
    best_eval_mate=None,
    profile_id=CANONICAL_PROFILE_ID,
):
    """Insert a trusted position_analysis storage winner (position-complete-v1)."""
    profile = get_profile(profile_id)
    identity = {f: getattr(profile, f) for f in IDENTITY_FIELDS}
    row = PositionAnalysisRow(
        normalized_fen=normalize_fen(fen),
        fen=fen,
        best_move_uci=best_move_uci,
        best_move_san=best_move_uci,
        best_line_uci=best_line_uci,
        best_eval=best_eval,
        best_eval_mate=best_eval_mate,
        source="precomputed",
        analysis_profile_id=profile_id,
        evidence_contract_id="position-complete-v1",
        **identity,
    )
    session.add(row)
    session.commit()
    return row


def test_root_eval_storage_winner_overrides_disagreeing_cache_sibling(session):
    # A trusted storage winner drives the root eval; a disagreeing (even trusted)
    # analysis_cache sibling is never consulted once storage resolves.
    _seed_position(session, fen=POS_A, best_eval=42)
    _seed(session, fen=POS_A, uci="f1c4", best_move_uci="f1c4",
          best_line_uci="f1c4 g8f6", best_eval=99)
    assert lookup_root_eval(session, POS_A) == MoveEval(cp=42, mate=None)


def test_root_eval_storage_miss_uses_trusted_legacy_excludes_untrusted(session):
    # No storage row: a trusted legacy v2 row supplies the root; an untrusted browser
    # sibling at the same position is filtered out before ranking.
    _seed(session, fen=POS_A, uci="f1c4", best_move_uci="f1c4",
          best_line_uci="f1c4 g8f6", best_eval=35)
    _seed(session, fen=POS_A, uci="c2c4", best_move_uci="c2c4",
          best_line_uci="c2c4 d7d5", best_eval=99, trusted=False,
          analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    assert lookup_root_eval(session, POS_A) == MoveEval(cp=35, mate=None)


def test_root_eval_ranks_at_normalized_grain_not_exact_fen_first(session):
    # Finding 4: the exact-FEN trusted row is CP-only; a trusted MATE row lives at a
    # clock variant (same normalized FEN, different full FEN). Normalized-grain
    # ranking must pick the mate row — the old exact-FEN-first behavior would have
    # wrongly returned the CP eval of the exact row.
    _seed(session, fen=POS_A, uci="f1c4", best_move_uci="f1c4",
          best_line_uci="f1c4 g8f6", best_eval=20)
    _seed(session, fen=POS_B, uci="f1c4", best_move_uci="f1c4",
          best_line_uci="f1c4 g8f6", best_eval_mate=3)
    assert lookup_root_eval(session, POS_A) == MoveEval(cp=None, mate=3)


def test_move_eval_mate_outranks_cp_among_trusted(session):
    # Move-grain mate ranking among trusted rows: a played-mate row outranks a
    # CP-only row at the same normalized position+move.
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10)
    _seed(
        session,
        fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
        uci="f1c4",
        played_eval=20,
        played_eval_mate=2,
    )
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=None, mate=2)


def test_untrusted_exact_row_surfaces_as_fallback(session):
    # An untrusted browser row on the EXACT key is not trusted, but with no trusted
    # alternative its played_eval is surfaced as the tier-3 fallback so the off-book
    # card shows a number instead of "—".
    _seed(session, fen=POS_A, uci="f1c4", played_eval=77, trusted=False,
          analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=77, mate=None)


def test_move_eval_trust_filter_lets_trusted_transposition_win(session):
    # Untrusted exact row is rejected in favor of a trusted transposition: tier 2
    # (trusted normalized) beats tier 3 (untrusted exact).
    _seed(session, fen=POS_A, uci="f1c4", played_eval=77, trusted=False,
          analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    _seed(session, fen=POS_B, uci="f1c4", played_eval=18)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=18, mate=None)


# --- untrusted played-eval fallback (tiers 3-4) -------------------------------
#
# These pin the g-a0ix change: when no trusted eval exists, an untrusted played eval
# (browser-game or ANY non-authoritative source) is surfaced so off-book cards show a
# number. Cache-row -> eval RESOLUTION lives here; eval -> tie-break sort ordering is
# owned by test_tree_api.py (which patches lookup_move_evals), so no sort test is
# added here for this change.

def test_untrusted_normalized_fallback_surfaces(session):
    # Tier 4: only an untrusted browser row exists, stored under a clock variant, so
    # the request resolves via the untrusted normalized fallback.
    _seed(session, fen=POS_B, uci="f1c4", played_eval=33, trusted=False,
          analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=33, mate=None)


def test_trusted_exact_beats_untrusted_normalized_fallback(session):
    # Tier 1 > tier 4. The trusted row goes on the exact key; the untrusted row on a
    # clock variant (same normalized) — analysis_cache's UniqueConstraint(fen_before,
    # move_uci) forbids two rows on the exact key, so the exact-key collision itself
    # is covered by the writer/upsert tests, not a dual-seed here.
    _seed(session, fen=POS_A, uci="f1c4", played_eval=18)
    _seed(session, fen=POS_B, uci="f1c4", played_eval=77, trusted=False,
          analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=18, mate=None)


def test_untrusted_fallback_ranks_precomputed_over_game(session):
    # Among untrusted survivors, _move_sort_key still applies: a precomputed-but-
    # untrusted row outranks a game-untrusted row (source_rank precomputed < game).
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10, source="game",
          trusted=False, analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    _seed(session, fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
          uci="f1c4", played_eval=20, source="precomputed",
          trusted=False, analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=20, mate=None)


def test_untrusted_fallback_prefers_mate_over_cp(session):
    # Mate data wins among untrusted survivors at the same normalized position+move.
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10, trusted=False,
          analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    _seed(session, fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
          uci="f1c4", played_eval=20, played_eval_mate=2, trusted=False,
          analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=None, mate=2)


def test_non_browser_untrusted_source_surfaces(session):
    # Tiers 3-4 are source-agnostic, NOT browser-specific: a bare untrusted row with
    # no profile/contract identity (source=None) still surfaces its played_eval.
    _seed(session, fen=POS_A, uci="f1c4", played_eval=44, source=None, trusted=False)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=44, mate=None)


def test_untrusted_fallback_ranks_analysis_over_game(session):
    # g-cache-stronger-evals: among untrusted tier-4 survivors at the same normalized
    # position+move, source_rank("analysis") < source_rank("game"), so an analysis
    # row outranks a game row when no exact untrusted row exists.
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10, source="game",
          trusted=False, analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    _seed(session, fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
          uci="f1c4", played_eval=20, source="analysis",
          trusted=False, analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=20, mate=None)


def test_exact_game_row_beats_normalized_analysis_transposition(session):
    # The analysis source rank only reorders tier 4 (normalized untrusted). An EXACT
    # untrusted game row (tier 3) still wins over a normalized analysis row (tier 4).
    _seed(session, fen=POS_A, uci="f1c4", played_eval=10, source="game",
          trusted=False, analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    _seed(session, fen=POS_B, uci="f1c4", played_eval=20, source="analysis",
          trusted=False, analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=10, mate=None)


def test_mixed_batch_trusted_untrusted_and_miss(session):
    # One node trusted (tier 1), one untrusted-only (tier 3), one genuine miss.
    _seed(session, fen=POS_A, uci="f1c4", played_eval=42)               # trusted exact
    _seed(session, fen=POS_A, uci="f1b5", played_eval=7, trusted=False,  # untrusted exact
          analysis_profile_id=BROWSER_PROFILE_ID,
          evidence_contract_id="resolver-complete-v1")
    out = lookup_move_evals(
        session, [(POS_A, "f1c4"), (POS_A, "f1b5"), (POS_A, "g1f3")]
    )
    assert out == {
        (POS_A, "f1c4"): MoveEval(cp=42, mate=None),
        (POS_A, "f1b5"): MoveEval(cp=7, mate=None),
        (POS_A, "g1f3"): None,
    }


# --- perspective conversion ----------------------------------------------------

def test_perspective_white_unchanged():
    ev = MoveEval(cp=30, mate=None)
    assert eval_for_perspective(ev, "white") is ev


def test_perspective_black_negates_cp():
    assert eval_for_perspective(MoveEval(cp=30, mate=None), "black") == MoveEval(cp=-30, mate=None)


def test_perspective_black_negates_mate():
    assert eval_for_perspective(MoveEval(cp=None, mate=4), "black") == MoveEval(cp=None, mate=-4)


def test_perspective_zero_and_none():
    assert eval_for_perspective(MoveEval(cp=0, mate=None), "black") == MoveEval(cp=0, mate=None)
    assert eval_for_perspective(None, "black") is None


def test_perspective_invalid_color():
    with pytest.raises(ValueError):
        eval_for_perspective(MoveEval(cp=1, mate=None), "sideways")


# --- writer populates the derived column ---------------------------------------

def test_writer_populates_normalized_fen_before(session):
    write_analysis_cache_rows(
        session,
        [
            {
                "fen_before": POS_A,
                "move_uci": "f1c4",
                "move_san": "Bc4",
                "played_eval": 25,
                "source": "game",
                # The file's ONLY writer-path test, so the only one the active gate
                # can refuse: browser-game-v1 is retired (g-bgv1-cutover) and would
                # never reach the derived-column write. Every _seed row above stays
                # on v1 on purpose — those pin that HISTORICAL v1 rows keep serving
                # tiers 3-4, since the fallback is trust-based, not active-based.
                "analysis_profile_id": JEFFML_PROFILE_ID,
                "evidence_contract_id": MINIMAL_PLAYED_EVAL,
            }
        ],
    )
    row = session.query(AnalysisCache).one()
    assert row.normalized_fen_before == normalize_fen(POS_A)
    # The row is a non-authoritative passive row. No trusted eval exists for this
    # position+move, so it resolves the clock-variant request via the untrusted
    # normalized fallback (tier 4) — surfacing the played_eval rather than
    # dropping it. (Trusted transposition resolution is covered by the fallback tests
    # above, which seed identity-bearing rows.)
    out = lookup_move_evals(session, [(POS_B, "f1c4")])
    assert out[(POS_B, "f1c4")] == MoveEval(cp=25, mate=None)
