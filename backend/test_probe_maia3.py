"""scripts/probe_maia3.py: pool filters and selection, the statistics, the decision rule.

No engine, no Stockfish and no network. The real engine runs only when the probe
itself runs (and in test_maia3_engine_release.py); Stockfish judges are replaced
by deterministic fakes here.
"""

from __future__ import annotations

import concurrent.futures
import math
import random
import threading
import time
import zlib

import chess
import pytest

from scripts import probe_maia3 as probe

QUALIFYING = {
    "Event": "Rated Blitz game",
    "Site": "https://lichess.org/abcdefgh",
    "UTCDate": "2025.10.01",
    "UTCTime": "00:00:08",
    "WhiteElo": "1500",
    "BlackElo": "1550",
    "TimeControl": "180+2",
    "Termination": "Normal",
}


def _headers(**changes: str | None) -> dict[str, str]:
    headers = dict(QUALIFYING)
    for key, value in changes.items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    return headers


def _movetext(plies: int, clock_s: float = 120.0, clocks: dict[int, float | None] | None = None,
              seed: int = 7) -> str:
    """A legal game of ``plies`` half-moves with a [%clk] after each (index -> override)."""
    clocks = clocks or {}
    board = chess.Board()
    rng = random.Random(seed)
    parts = []
    for i in range(plies):
        moves = sorted(board.legal_moves, key=lambda m: m.uci())
        rng.shuffle(moves)
        move = next(m for m in moves if not _ends(board, m))
        number = f"{board.fullmove_number}." if board.turn == chess.WHITE else f"{board.fullmove_number}..."
        san = board.san(move)
        board.push(move)
        clock = clocks.get(i, clock_s)
        comment = "" if clock is None else f" {{ [%clk 0:{int(clock) // 60:02d}:{int(clock) % 60:02d}] }}"
        parts.append(f"{number} {san}{comment}")
    return " ".join(parts) + " 1-0"


def _ends(board: chess.Board, move: chess.Move) -> bool:
    board.push(move)
    try:
        return board.is_game_over()
    finally:
        board.pop()


def _game(site: str = "a", plies: int = 70, band_elo: str = "1500", **kw) -> probe.PgnGame:
    headers = _headers(Site=f"https://lichess.org/{site}", WhiteElo=band_elo, BlackElo=band_elo)
    return probe.PgnGame(headers, _movetext(plies, seed=zlib.crc32(site.encode()) % 1000, **kw))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_a_rated_blitz_game_between_two_band_players_qualifies():
    assert probe.qualify(_headers()) == 1500


@pytest.mark.parametrize(
    "time_control, blitz",
    [("180+0", True), ("60+3", True), ("479+0", True), ("300+3", True),
     ("179+0", False), ("120+1", False), ("480+0", False), ("600+0", False), ("-", False)],
)
def test_blitz_is_base_plus_40_increment_from_180_to_479_seconds(time_control, blitz):
    assert (probe.qualify(_headers(TimeControl=time_control)) is not None) is blitz


@pytest.mark.parametrize(
    "changes",
    [
        {"Event": "Casual Blitz game"},
        {"WhiteTitle": "BOT"},
        {"BlackTitle": "BOT"},
        {"FEN": "8/8/8/8/8/8/8/8 w - - 0 1"},
        {"Variant": "Chess960"},
        {"Termination": "Rules infraction"},
        {"BlackElo": "1601"},
        {"WhiteElo": "1100"},
        {"WhiteElo": None},
        {"BlackElo": "?"},
    ],
)
def test_games_outside_the_filters_do_not_qualify(changes):
    assert probe.qualify(_headers(**changes)) is None


def test_band_edges_are_inclusive():
    assert probe.qualify(_headers(WhiteElo="1400", BlackElo="1600")) == 1500
    assert probe.qualify(_headers(WhiteElo="2500", BlackElo="2700")) == 2600
    assert probe.qualify(_headers(WhiteElo="1399", BlackElo="1500")) is None


def test_parse_movetext_reads_moves_and_clocks():
    sans, clocks = probe.parse_movetext(
        "1. e4 { [%clk 0:03:00] } 1... e5 { [%eval 0.2] [%clk 0:02:59.5] } 2. Nf3 $1 2... Nc6 { [%clk 0:02:58] } 1/2-1/2"
    )
    assert sans == ["e4", "e5", "Nf3", "Nc6"]
    assert clocks == [180.0, 179.5, None, 178.0]


def test_a_candidate_is_one_legal_position_in_the_ply_range():
    game = _game("x1")
    candidate = probe.candidate_from_game(game, 1500, seed=1, game_index=0)

    assert candidate is not None
    assert probe.PLY_RANGE[0] <= len(candidate.moves) <= probe.PLY_RANGE[1]
    board = chess.Board()
    for uci in candidate.moves:
        board.push_uci(uci)
    assert chess.Move.from_uci(candidate.human) in board.legal_moves
    # The draw depends on the game and the seed, not on processing order.
    assert probe.candidate_from_game(game, 1500, seed=1, game_index=99).moves == candidate.moves


def test_the_ply_is_drawn_among_plies_the_human_moved_from():
    # 17 half-moves: the only position with a human move after it at ply >= 16 is ply 16.
    candidate = probe.candidate_from_game(_game("short", plies=17), 1500, seed=3, game_index=0)
    assert candidate is not None and len(candidate.moves) == 16
    assert probe.candidate_from_game(_game("shorter", plies=16), 1500, seed=3, game_index=0) is None


def test_low_or_missing_mover_clock_skips_the_game():
    # With 17 plies the position is at ply 16; the mover's previous move is half-move 15 (index 14).
    low = _game("low", plies=17, clocks={14: 29.0})
    missing = _game("missing", plies=17, clocks={14: None})
    opponent_low = _game("opp", plies=17, clocks={15: 5.0})

    assert probe.candidate_from_game(low, 1500, seed=3, game_index=0) is None
    assert probe.candidate_from_game(missing, 1500, seed=3, game_index=0) is None
    assert probe.candidate_from_game(opponent_low, 1500, seed=3, game_index=0) is not None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class _ShuffledExecutor(concurrent.futures.ThreadPoolExecutor):
    """Judges finish out of order: each sleeps a random few milliseconds."""

    def __init__(self) -> None:
        super().__init__(max_workers=6)
        self._rng = random.Random(5)
        self._lock_rng = threading.Lock()

    def submit(self, fn, *args):
        with self._lock_rng:
            delay = self._rng.random() / 200

        def delayed():
            time.sleep(delay)
            return fn(*args)

        return super().submit(delayed)


def _stream(n_per_band: int) -> list[probe.PgnGame]:
    games = []
    for i in range(n_per_band):
        for band in probe.BANDS:
            games.append(_game(f"{band}-{i}", plies=40, band_elo=str(band)))
    return games


def _passes(candidate: probe.Candidate) -> bool:
    return candidate.game_index % 3 != 0


def test_selection_is_the_first_passing_candidates_in_stream_order():
    games = _stream(30)
    with _ShuffledExecutor() as executor:
        build = probe.build_pool(
            games,
            judge_natural=lambda c: (_passes(c), [("row", c.game_index)]),
            judge_tactical=lambda c: (c.game_index % 2 == 0, []),
            executor=executor,
            seed=1,
            per_band=5,
            tactical_per_band=2,
            band_cap=8,
            tactical_cap=10,
        )

    expected_tactical = []
    indexes_by_band = {}
    for band in probe.BANDS:
        indexes = [i for i, g in enumerate(games) if g.headers["WhiteElo"] == str(band)]
        indexes_by_band[band] = indexes
        # Routing: the first band_cap of a band's games are natural candidates,
        # the band's later games its only-move candidates.
        expected = [i for i in indexes[:8] if i % 3 != 0][:5]
        assert [c.game_index for c in build.natural[band]] == expected
        expected_tactical += [i for i in indexes[8:] if i % 2 == 0][:2]
    tactical = [c.game_index for c in build.tactical]
    assert tactical == expected_tactical
    assert build.games_scanned == max(tactical + [c.game_index for cs in build.natural.values() for c in cs]) + 1
    # Each band records its own window and how many candidates it judged.
    for band in probe.BANDS:
        window = build.natural_windows[band]
        chosen = build.natural[band]
        assert window["games_scanned"] == chosen[-1].game_index + 1
        judged = [i for i in indexes_by_band[band][:8] if i <= chosen[-1].game_index]
        assert window["judged"] == len(judged)
        assert window["rejected"] == len(judged) - len(chosen)


def test_a_selector_wants_the_stream_until_it_holds_its_reserve():
    selector = probe.OrderedSelector(target=4, cap=100)
    reserve = math.ceil(probe.CANDIDATE_RESERVE * 4)
    candidate = probe.Candidate(600, ("e2e4",), "e7e5", 0, "?")
    for _ in range(4 + reserve - 1):
        selector.add(candidate)
    assert selector.needs_stream()
    selector.add(candidate)
    assert not selector.needs_stream()

    for index in selector.to_submit():  # two rejections eat into the reserve
        selector.record(index, passed=index >= 2)
    assert selector.needs_stream()


def test_a_short_stream_raises_pool_short():
    with concurrent.futures.ThreadPoolExecutor(2) as executor, pytest.raises(probe.PoolShort):
        probe.build_pool(
            _stream(3),
            judge_natural=lambda c: (True, []),
            judge_tactical=lambda c: (True, []),
            executor=executor,
            seed=1,
            per_band=5,
            tactical_per_band=1,
        )


def test_opening_positions_are_distinct_book_positions_in_the_ply_range():
    positions = probe.opening_positions(probe.ECO_PATH, seed=1)

    assert len(positions) == probe.OPENING_COUNT
    for position in positions:
        probe.check_position(position)
    assert len({p.board().epd() for p in positions}) == probe.OPENING_COUNT
    assert all(probe.OPENING_PLIES[0] <= len(p.moves) <= probe.OPENING_PLIES[1] for p in positions)
    assert probe.opening_positions(probe.ECO_PATH, seed=1) == positions


def test_hand_picked_tactics_are_legal_positions():
    for position in probe.hand_tactical_positions():
        probe.check_position(position)


# ---------------------------------------------------------------------------
# The committed fixture
# ---------------------------------------------------------------------------


def test_the_committed_fixture_has_its_pools_and_provenance():
    header, positions = probe.load_fixture()  # checks every move's legality

    for key in ("source_url", "games_scanned", "stream_utc_range", "seed", "stockfish"):
        assert header[key], key
    for band in probe.BANDS:
        window = header["natural_by_band"][str(band)]
        assert window["candidates_judged"] - window["rejected_decided"] == probe.NATURAL_PER_BAND
        assert window["games_scanned"] <= header["games_scanned"]
        assert header["stream_utc_range"][0] <= window["utc_range"][0] <= window["utc_range"][1]
    by_pool = {pool: [p for p in positions if p.pool == pool] for pool in probe.POOLS}
    by_band = probe.natural_by_band(positions)
    assert {band: len(rows) for band, rows in by_band.items()} == {
        band: probe.NATURAL_PER_BAND for band in probe.BANDS
    }
    assert len(by_pool["tactical"]) == len(probe.HAND_TACTICS) + probe.TACTICAL_AUTO
    assert len(by_pool["opening"]) == probe.OPENING_COUNT
    assert len(probe.headline1_set(positions)) == 5 * probe.H1_PER_BAND + 70
    for position in by_pool["natural"]:
        assert probe.PLY_RANGE[0] <= len(position.moves) <= probe.PLY_RANGE[1]


# ---------------------------------------------------------------------------
# Runs and CPL
# ---------------------------------------------------------------------------


def test_grid_positions_get_the_full_grid_and_the_rest_their_own_band(monkeypatch):
    monkeypatch.setattr(probe, "H1_PER_BAND", 1)
    firsts = [
        probe.Position("natural", (first,), band, "g8f6")
        for first, band in zip(("e2e4", "d2d4", "c2c4", "g1f3", "b1c3"), probe.BANDS)
    ]
    own = probe.Position("natural", ("e2e4", "e7e5"), 600, "g1f3")
    tactical = probe.Position("tactical", ("f2f4",), label="t")
    positions = firsts + [own, tactical]

    tasks = probe.plan_engine_tasks(positions, positions)

    grid = len(probe.GRID) + len(probe.BANDS) * probe.K_H1
    by_position = {p.key: [t for t in tasks if t.pos.key == p.key] for p in positions}
    assert all(len(by_position[p.key]) == grid for p in firsts + [tactical])
    assert sorted((t.setting, t.elo, t.sample) for t in by_position[own.key]) == sorted(
        [("T0", 600, 0)] + [("T1", 600, s) for s in range(probe.K_H2)]
    )


def _cache_with(tmp_path, root_best: str, children: dict[str, int]) -> probe.ProbeCache:
    cache = probe.ProbeCache(tmp_path / "cache.sqlite3")
    rows = [("SF", probe.SF_DEPTH, "e2e4", "", 30, root_best)]
    rows += [("SF", probe.SF_DEPTH, "e2e4", move, score, None) for move, score in children.items()]
    cache.add_evals(rows)
    return cache


def test_loss_is_best_minus_move_clamped_with_mates_as_1000(tmp_path):
    cache = _cache_with(
        tmp_path,
        "d8h4",
        {"d8h4": probe.MATE_CP - 3, "e7e5": 300, "g8f6": 2500, "f7f6": -probe.MATE_CP + 2},
    )
    results = probe.Results(cache, "m", "SF")
    position = probe.Position("tactical", ("e2e4",))

    assert results.loss(position, "d8h4") == 0
    assert results.loss(position, "e7e5") == 700
    assert results.loss(position, "g8f6") == 0  # both clamp to +1000
    assert results.loss(position, "f7f6") == 1000


def test_a_move_scored_above_the_best_move_loses_nothing(tmp_path):
    cache = _cache_with(tmp_path, "e7e5", {"e7e5": 20, "c7c5": 35})
    results = probe.Results(cache, "m", "SF")

    assert results.loss(probe.Position("natural", ("e2e4",), 1500, "c7c5"), "c7c5") == 0


def test_only_move_gap_uses_clamped_scores():
    assert probe.only_move_gap([probe.MATE_CP - 1, 355]) == 645
    assert probe.only_move_gap([1500, 1300]) == 0
    assert probe.only_move_gap([40]) is None


# ---------------------------------------------------------------------------
# Statistics and the decision rule
# ---------------------------------------------------------------------------


def test_pav_fits_a_non_increasing_curve():
    third = 10 / 3
    assert probe.pav_nonincreasing([5, 4, 4.5, 3, 3.5, 3.5, 1]) == pytest.approx(
        [5, 4.25, 4.25, third, third, third, 1]
    )
    fitted = probe.pav_nonincreasing([random.Random(i).random() for i in range(30)])
    assert all(a >= b for a, b in zip(fitted, fitted[1:]))


def test_crossing_elo_interpolates_and_reports_out_of_range():
    grid = [600, 700, 800, 900]
    fitted = [100.0, 80.0, 80.0, 60.0]

    assert probe.crossing_elo(grid, fitted, 90.0) == 650
    assert probe.crossing_elo(grid, fitted, 80.0) == 700  # the smallest Elo on a flat stretch
    assert probe.crossing_elo(grid, fitted, 70.0) == 850
    assert probe.crossing_elo(grid, fitted, 120.0) == -math.inf
    assert probe.crossing_elo(grid, fitted, 50.0) == math.inf


def test_loglinear_fit_and_the_elo_offset_sign():
    beta = -0.0006
    ys = [math.exp(5.0 + beta * x) for x in probe.BANDS]
    alpha, fitted_beta, residuals = probe.loglinear_fit(probe.BANDS, ys)

    assert alpha == pytest.approx(5.0) and fitted_beta == pytest.approx(beta)
    assert max(abs(r) for r in residuals) < 1e-9
    # Losing less than the humans of the band reads as stronger than the label.
    assert probe.elo_offset(math.exp(beta * 200) * 50, 50, beta) == pytest.approx(200)
    assert probe.elo_offset(60, 50, beta) < 0


def _decide(
    e1: list[float], e0: list[float], e1_upper: list[float] | None = None, beta_upper: float = -0.0004
):
    bands = probe.BANDS
    upper = e1_upper or [e + 200 for e in e1]
    return probe.decide(
        dict(zip(bands, e1)), dict(zip(bands, e0)), dict(zip(bands, upper)),
        {b: 110.0 for b in bands}, {b: 100.0 for b in bands}, 0.7, beta_upper,
    )


def test_the_rule_keeps_t1_when_it_is_closer_and_no_band_is_clearly_low():
    decision = _decide([-100] * 5, [333, -20, -220, 150, 250])
    assert decision.criterion1 and decision.criterion2
    assert decision.recommendation.startswith("keep T=1")
    assert decision.d1 == 100 and decision.d0 == pytest.approx(194.6)
    assert decision.f1 == pytest.approx(probe.FOLDED_NORMAL * 110)


def test_the_rule_recommends_t0_when_t1_is_farther_from_its_labels():
    decision = _decide([-300] * 5, [50, -50, 50, -50, 50])
    assert not decision.criterion1
    assert decision.recommendation.startswith("T=0")


def test_a_clearly_low_band_keeps_t1_with_a_follow_up():
    decision = _decide([-100] * 5, [300] * 5, e1_upper=[50, 50, -160, 50, 50])
    assert decision.criterion1 and not decision.criterion2
    assert decision.low_bands == (1500,)
    assert "follow-up for bands 1500" in decision.recommendation


def test_the_rule_recommends_nothing_when_the_human_slope_may_be_zero():
    decision = _decide([-100] * 5, [333, -20, -220, 150, 250], beta_upper=0.0001)
    assert not decision.fit_usable
    assert decision.recommendation.startswith("none")


class _FakeResults:
    """Per-position losses: human h, T=0 h*k0, T=1 h*k1 (by band)."""

    def __init__(self, k1: dict[int, float], k0: dict[int, float], beta: float) -> None:
        self.k1, self.k0, self.beta = k1, k0, beta

    def loss(self, position, move):
        return position.human_loss

    def t0_loss(self, position, elo):
        return position.human_loss * self.k0[elo]

    def t1_losses(self, position, elo, k):
        return [position.human_loss * self.k1[elo]] * k


class _P:
    def __init__(self, band: int, loss: float) -> None:
        self.band, self.human_loss, self.human = band, loss, "x"


def test_headline2_reads_maia_in_elo_through_the_human_slope():
    beta = -0.0006
    rng = random.Random(1)
    by_band = {
        b: [_P(b, math.exp(5 + beta * b) * rng.uniform(0.5, 1.5)) for _ in range(200)]
        for b in probe.BANDS
    }
    # T=1 matches the humans; T=0 loses as much as humans 300 Elo stronger.
    k1 = {b: 1.0 for b in probe.BANDS}
    k0 = {b: math.exp(beta * 300) for b in probe.BANDS}
    out = probe.headline2(_FakeResults(k1, k0, beta), by_band, random.Random(2))

    for band in probe.BANDS:
        assert out["bands"][band]["e1"] == pytest.approx(0, abs=1e-6)
        assert out["bands"][band]["e0"] == pytest.approx(300, rel=0.05)
    assert out["decision"]["fit_usable"]
    assert out["decision"]["criterion1"] and out["decision"]["criterion2"]


def test_headline2_makes_no_recommendation_from_a_flat_human_fit():
    rng = random.Random(1)
    by_band = {b: [_P(b, 60 * rng.uniform(0.5, 1.5)) for _ in range(200)] for b in probe.BANDS}
    k = {b: 1.1 for b in probe.BANDS}
    out = probe.headline2(_FakeResults(k, k, 0.0), by_band, random.Random(2))

    assert out["beta_interval"][1] > 0  # humans do not get better with rating here
    assert not out["decision"]["fit_usable"]
    assert out["decision"]["recommendation"].startswith("none")
