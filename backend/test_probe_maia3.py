"""scripts/probe_maia3.py: pool filters and selection, the statistics, the decision rule.

No engine, no Stockfish and no network. The real engine runs only when the probe
itself runs (and in test_maia3_engine_release.py); Stockfish judges are replaced
by deterministic fakes here.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
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


# ---------------------------------------------------------------------------
# Temperature per rating (g-maia3-elo-temp): settings and plans
# ---------------------------------------------------------------------------

CURVE = ((600, 0.2), (2600, 1.0))


def test_setting_names_are_the_cache_keys_and_read_back():
    settings = [
        probe.Setting(temperature=0.0),
        probe.Setting(temperature=1.0),
        probe.Setting(temperature=0.35),
        probe.Setting(temperature=1.0, top_p=0.9),
        probe.Setting(curve=CURVE),
    ]

    # "T0" and "T1" are the 2026-11-18 run's names, so its cached moves are reused.
    assert [s.name for s in settings] == ["T0", "T1", "T0.35", "T1P0.9", "C:600:0.2,2600:1.0"]
    assert [probe.Setting.from_name(s.name) for s in settings] == settings
    with pytest.raises(ValueError):
        probe.Setting(temperature=1.0, curve=CURVE)
    with pytest.raises(ValueError):
        probe.Setting()


def test_run_settings_for_a_flat_a_top_p_and_a_curve_setting(tmp_path):
    flat = probe.run_settings("maia3-23m", probe.Setting(temperature=0.35), 1, tmp_path)
    nucleus = probe.run_settings("maia3-23m", probe.Setting(temperature=1.0, top_p=0.9), 1, tmp_path)
    curve = probe.run_settings("maia3-23m", probe.Setting(curve=CURVE), 1, tmp_path)

    assert (flat.temperature_for(600), flat.temperature_for(2600), flat.top_p) == (0.35, 0.35, 1.0)
    assert (nucleus.temperature_for(2600), nucleus.top_p) == (1.0, 0.9)
    # The curve is the production path: no flat temperature at all.
    assert curve.temperature is None
    assert [curve.temperature_for(elo) for elo in (600, 1600, 2600)] == [0.2, 0.6, 1.0]


def _synthetic_pool() -> tuple[dict[int, list[probe.Position]], list[probe.Position]]:
    firsts = iter("e2e4 d2d4 c2c4 g1f3 b1c3".split())
    natural = {
        1500: [probe.Position("natural", (next(firsts),), 1500, "g8f6", mover=i) for i in range(3)],
        2300: [probe.Position("natural", (next(firsts),), 2300, "g8f6", mover=i) for i in range(2)],
    }
    opening = [probe.Position("opening", ("f2f4",), label="a"), probe.Position("opening", ("b2b3",), label="b")]
    return natural, opening


def _count(tasks, **where) -> int:
    def matches(task) -> bool:
        return all(
            (task.pos.pool if key == "pool" else getattr(task, key)) == value for key, value in where.items()
        )

    return sum(1 for task in tasks if matches(task))


def test_the_sweep_plan_samples_each_group_as_the_fit_reads_it(monkeypatch):
    monkeypatch.setattr(probe, "H1_PER_BAND", 1)
    natural, opening = _synthetic_pool()

    tasks = probe.plan_sweep_tasks(natural, opening)

    assert len({t.key for t in tasks}) == len(tasks)
    names = [probe.Setting(temperature=t).name for t in probe.SWEEP_TEMPERATURES]
    for name in names[1:]:
        # The first position of a band is a variety position; so is every opening.
        assert _count(tasks, setting=name, elo=1500, pool="natural") == probe.K_H1 + 2 * probe.K_H2
        assert _count(tasks, setting=name, elo=1500, pool="opening") == 2 * probe.K_H1
        assert _count(tasks, setting=name, elo=2300, pool="natural") == probe.K_H1 + probe.K_H2
    # T=0 always plays the top move: one move per natural position, none on openings.
    assert _count(tasks, setting="T0") == 5
    assert {t.sample for t in tasks if t.setting == "T0"} == {0}
    assert _count(tasks, setting="T0", pool="opening") == 0
    # The TopP arms run at their bands only.
    arms = [t for t in tasks if "P" in t.setting]
    assert {t.setting for t in arms} == {"T1P0.9", "T1P0.8"}
    assert {t.elo for t in arms} == {2300}
    # Every natural position is played at its own band's Elo only.
    assert all(t.elo == t.pos.band for t in tasks if t.pos.pool == "natural")


def test_the_validation_plan_covers_the_rule(monkeypatch):
    monkeypatch.setattr(probe, "H1_PER_BAND", 1)
    monkeypatch.setattr(probe, "LADDER_PER_BAND", 2)
    natural, opening = _synthetic_pool()
    curve = probe.Setting(curve=CURVE)

    tasks = probe.plan_validation_tasks(natural, opening, curve)

    assert len({t.key for t in tasks}) == len(tasks)
    assert _count(tasks, setting="T1") == 5 * probe.K_H2 and _count(tasks, setting="T0") == 5
    assert all(t.elo == t.pos.band for t in tasks if t.setting in ("T0", "T1"))
    # R3: the opening pool under the curve at every band and every bot level.
    elos = sorted({t.elo for t in tasks if t.pos.pool == "opening"})
    assert elos == sorted(set(probe.ELO_BINS) | {1500, 2300})
    for elo in elos:
        assert _count(tasks, setting=curve.name, elo=elo, pool="opening") == 2 * probe.K_H1
    # R4: the first LADDER_PER_BAND positions of each band at all 16 levels.
    for rows in natural.values():
        for position in rows[:2]:
            mine = [t for t in tasks if t.pos.key == position.key and t.setting == curve.name]
            for level in probe.ELO_BINS:
                assert {t.sample for t in mine if t.elo == level} >= set(range(probe.K_H2))
    # A position beyond the ladder is played at its own band's Elo only.
    third = natural[1500][2]
    assert {t.elo for t in tasks if t.pos.key == third.key} == {1500}
    # Only natural positions go to Stockfish.
    assert {p.pool for p in probe.natural_positions(tasks)} == {"natural"}


def test_a_pilot_takes_a_few_positions_of_every_kind_with_all_their_moves(monkeypatch):
    monkeypatch.setattr(probe, "H1_PER_BAND", 1)
    natural, opening = _synthetic_pool()
    tasks = probe.plan_sweep_tasks(natural, opening)

    pilot, groups = probe.pilot_split(tasks, per_group=1)

    # 1500: its variety position and one of the other two; 2300: both kinds; one opening.
    chosen = {t.pos.key for t in pilot}
    assert chosen == {"e2e4", "d2d4", "g1f3", "b1c3", "f2f4"}
    assert sorted(len(rows) for rows in groups.values()) == [1, 1, 1, 2, 2]
    assert _count(pilot, pool="natural") == sum(1 for t in tasks if t.pos.key in chosen and t.pos.pool == "natural")


# ---------------------------------------------------------------------------
# The fit: crossing, floor, anchor
# ---------------------------------------------------------------------------

GRID = probe.SWEEP_TEMPERATURES


def test_pav_fits_a_non_decreasing_curve():
    assert probe.pav_nondecreasing([1, 3, 2, 4]) == [1, 2.5, 2.5, 4]
    assert probe.pav_nondecreasing([1, 2, 3]) == [1, 2, 3]


def test_the_crossing_is_the_highest_temperature_that_loses_no_more_than_the_humans():
    fitted = [20, 30, 40, 50, 60, 70, 80, 90]

    assert probe.crossing_temperature(GRID, fitted, 45) == pytest.approx(0.425)  # inside the grid
    assert probe.crossing_temperature(GRID, fitted, 10) == 0.0  # even T=0 loses more
    assert probe.crossing_temperature(GRID, fitted, 95) == 1.0  # T=1 still loses less: capped
    assert probe.crossing_temperature(GRID, fitted, 90) == 1.0
    # Across a flat stretch at the target: its upper end.
    flat = [20, 30, 45, 45, 45, 70, 80, 90]
    assert probe.crossing_temperature(GRID, flat, 45) == pytest.approx(0.65)


def test_the_floor_is_where_the_slower_pool_reaches_two_distinct_moves():
    floor = probe.floor_temperature(
        GRID,
        {
            "natural": [1.0, 1.2, 1.6, 2.4, 3.0, 3.5, 4.0, 4.5],  # crosses 2.0 at 0.35 + 0.15 / 2
            "opening": [1.0, 1.5, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0],  # crosses at 0.2 + 0.15 / 2
        },
    )

    assert floor["pools"]["natural"]["crossing"] == pytest.approx(0.425)
    assert floor["pools"]["opening"]["crossing"] == pytest.approx(0.275)
    assert floor["floor"] == pytest.approx(0.425)
    assert not floor["below_grid"] and not floor["not_reachable"]


def test_a_dip_in_variety_is_noise_and_gives_one_crossing():
    # Reaches 2.0 at 0.5, dips under it at 0.65 and rises again.
    noisy = [1.0, 1.2, 1.6, 2.0, 1.8, 2.6, 3.0, 3.4]

    floor = probe.floor_temperature(GRID, {"natural": noisy})

    fitted = floor["pools"]["natural"]["fitted"]
    assert fitted == sorted(fitted) and fitted[3] == fitted[4] == pytest.approx(1.9)
    # The fit is 1.9 at 0.65 and 2.6 at 0.8: one crossing, a seventh of the way.
    assert floor["floor"] == pytest.approx(0.65 + 0.15 / 7)


def test_a_floor_under_the_first_sampled_temperature_counts_as_that_temperature():
    floor = probe.floor_temperature(GRID, {"natural": [1.0, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]})

    assert floor["pools"]["natural"]["crossing"] == pytest.approx(0.1)
    assert floor["floor"] == 0.2 and floor["below_grid"]


def test_a_pool_short_of_variety_at_temperature_1_makes_the_floor_1():
    floor = probe.floor_temperature(
        GRID,
        {"natural": [1.0, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0], "opening": [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.9]},
    )

    assert floor["floor"] == 1.0 and floor["not_reachable"] and not floor["below_grid"]


VARIED = {"natural": [1.0, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]}  # floor 0.2


def test_fit_band_takes_the_crossing_when_no_floor_binds():
    fit = probe.fit_band(GRID, [20, 30, 40, 50, 60, 70, 80, 90], 46, VARIED)

    assert fit["t_star"] == pytest.approx(0.44)  # 6/10 of the way from 0.35 to 0.5
    assert (fit["anchor"], fit["set_by"], fit["bound"]) == (0.44, "crossing", None)
    assert fit["floor"]["floor"] == 0.2


def test_fit_band_caps_at_temperature_1_and_names_the_bounds():
    stronger = probe.fit_band(GRID, [20, 30, 40, 50, 60, 70, 80, 90], 95, VARIED)
    weaker = probe.fit_band(GRID, [20, 30, 40, 50, 60, 70, 80, 90], 10, VARIED)

    assert (stronger["anchor"], stronger["set_by"]) == (1.0, "cap")
    assert stronger["bound"] == "T=1 loses no more than the humans"
    # Even T=0 is weaker than the humans: the crossing is 0 and the floor sets the anchor.
    assert (weaker["t_star"], weaker["anchor"], weaker["set_by"]) == (0.0, 0.2, "floor")
    assert weaker["bound"] == "even T=0 loses more than the humans"


def test_a_noisy_cpl_series_is_fitted_before_the_crossing():
    # 0.5 loses less than 0.35 by chance; the fit pools them at 45.
    fit = probe.fit_band(GRID, [20, 30, 50, 40, 60, 70, 80, 90], 45, VARIED)

    assert fit["cpl_fitted"][2] == fit["cpl_fitted"][3] == 45
    assert fit["t_star"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    "t_star, floor, expected",
    [
        (0.1, 0.3512, (0.36, "floor")),  # a binding floor rounds up
        (0.1, 0.35, (0.35, "floor")),  # exactly 0.35 stays, despite 0.35 * 100 > 35 in floats
        (0.354, 0.2, (0.35, "crossing")),  # the crossing rounds to the nearest
        (0.356, 0.3512, (0.36, "crossing")),  # equal after rounding: the crossing
        (0.9, 1.0, (1.0, "floor")),
        (1.0, 0.2, (1.0, "cap")),
    ],
)
def test_the_anchor_never_rounds_under_a_binding_floor(t_star, floor, expected):
    assert probe.anchor_temperature(t_star, floor) == expected


# ---------------------------------------------------------------------------
# Movers and the cluster bootstrap
# ---------------------------------------------------------------------------


def _band(movers: list[int | None], **columns) -> probe.BandSample:
    return probe.BandSample(dict(columns), movers)


def test_mover_summary_counts_players_and_their_concentration():
    rows = [probe.Position("natural", ("e2e4",), 600, "e7e5", mover=m) for m in (0, 0, 0, 1, 2, 2, 3, 4, 5, 6)]

    assert probe.mover_summary(rows) == {
        "positions": 10, "movers": 7, "busiest_share": 0.3, "repeated_share": 0.5,
    }
    assert probe.mover_summary([probe.Position("natural", ("e2e4",), 600, "e7e5")]) is None


def test_the_cluster_bootstrap_keeps_one_movers_positions_together():
    # Positions 0 and 1 belong to one mover: each draw holds them equally often.
    sample = {600: _band([7, 7, 8, 9], first=[1, 0, 0, 0], second=[0, 1, 0, 0])}

    together = list(probe.resample_sums(sample, 200, random.Random(3), by_mover=True))
    apart = list(probe.resample_sums(sample, 200, random.Random(3), by_mover=False))

    assert all(sums["first"] == sums["second"] for _n, sums in (draw[600] for draw in together))
    assert any(sums["first"] != sums["second"] for _n, sums in (draw[600] for draw in apart))
    # A draw of three movers holds between three and six positions.
    assert {n for n, _sums in (draw[600] for draw in together)} <= {3.0, 4.0, 5.0, 6.0}
    assert all(n == 4 for n, _sums in (draw[600] for draw in apart))


def test_positions_without_a_mover_index_are_resampled_one_by_one():
    sample = {600: _band([None, None, None], x=[1.0, 2.0, 3.0])}

    assert sample[600].clusters(by_mover=True) == [[0], [1], [2]]
    means = [draw[600]["x"] for draw in probe.resample_means(sample, 50, random.Random(1))]
    assert min(means) >= 1.0 and max(means) <= 3.0 and len(set(means)) > 1


# ---------------------------------------------------------------------------
# The ladder (R4)
# ---------------------------------------------------------------------------


def _ladder(levels=probe.ELO_BINS, per_band: int = 150, seed: int = 11, slope: float = -0.0008):
    """Two bands of positions; each level's loss falls with its Elo, with its own noise."""
    rng = random.Random(seed)
    samples = {}
    for band in (1100, 2000):
        base = [5 + rng.expovariate(1 / 40) for _ in range(per_band)]
        samples[band] = probe.BandSample(
            {
                level: [b * math.exp(slope * (level - 600)) * rng.lognormvariate(0, 0.3) for b in base]
                for level in levels
            },
            [i // 2 for i in range(per_band)],  # two positions per mover
        )
    return samples


def _gap(ladder: dict, low: int, high: int) -> dict:
    return next(g for g in ladder["gaps"] if (g["lower"], g["higher"]) == (low, high))


def test_a_ladder_that_climbs_has_no_reversal():
    ladder = probe.ladder_gaps(_ladder(), probe.ELO_BINS, random.Random(1), resamples=500)

    assert len(ladder["gaps"]) == 120
    assert all(g["bound"] < 0 for g in ladder["gaps"])
    assert ladder["reversed"] == []
    assert ladder["positions"] == 300
    assert ladder["mean_cpl"][600] > ladder["mean_cpl"][2600]


def test_a_level_clearly_worse_than_a_lower_one_fails_r4():
    samples = _ladder()
    for sample in samples.values():
        sample.columns[1700] = [3 * v for v in sample.columns[1700]]

    ladder = probe.ladder_gaps(samples, probe.ELO_BINS, random.Random(1), resamples=500)

    assert ladder["reversed"]
    assert all(high == 1700 for _low, high in ladder["reversed"])
    assert [1600, 1700] in ladder["reversed"]


def test_one_pair_past_its_own_interval_is_not_a_clear_reversal_among_120():
    samples = _ladder()
    first = probe.ladder_gaps(samples, probe.ELO_BINS, random.Random(1), resamples=500)
    gap = _gap(first, 1600, 1700)
    # Move 1700 so that it sits 2.4 standard errors below 1600: scaling a level
    # shifts d and leaves its spread alone.
    factor = math.exp(2.4 * gap["s"] - gap["d"])
    for sample in samples.values():
        sample.columns[1700] = [factor * v for v in sample.columns[1700]]

    ladder = probe.ladder_gaps(samples, probe.ELO_BINS, random.Random(1), resamples=500)

    gap = _gap(ladder, 1600, 1700)
    assert gap["d"] == pytest.approx(2.4 * gap["s"])
    assert gap["d"] - 1.96 * gap["s"] > 0  # its own 95% interval is above 0
    assert ladder["c"] > 2.4 and gap["bound"] < 0  # the simultaneous bound is not
    assert [1600, 1700] not in ladder["reversed"]


def test_c_is_the_ordinary_one_sided_bound_for_one_pair_and_grows_with_more():
    samples = _ladder()
    alone = probe.ladder_gaps(samples, [1600, 1700], random.Random(1), resamples=500)
    everyone = probe.ladder_gaps(samples, probe.ELO_BINS, random.Random(1), resamples=500)

    # The pair's own studentized deviations, from the same draws.
    gap = alone["gaps"][0]
    deviations = []
    for draw in probe.resample_sums(samples, 500, random.Random(1)):
        low = sum(sums[1600] for _n, sums in draw.values())
        high = sum(sums[1700] for _n, sums in draw.values())
        deviations.append((math.log(high / low) - gap["d"]) / gap["s"])
    assert alone["c"] == pytest.approx(probe.percentile(deviations, 0.95))
    assert 1.2 < alone["c"] < 2.1  # near the normal 1.64
    assert everyone["c"] >= alone["c"]
    assert _gap(everyone, 1600, 1700)["s"] == pytest.approx(gap["s"])


def test_two_levels_that_play_the_same_moves_cannot_fail():
    samples = _ladder(levels=(1000, 1100))
    for sample in samples.values():
        sample.columns[1100] = list(sample.columns[1000])

    ladder = probe.ladder_gaps(samples, [1000, 1100], random.Random(1), resamples=100)

    gap = ladder["gaps"][0]
    assert (gap["d"], gap["s"], gap["bound"], ladder["c"]) == (0.0, 0.0, 0.0, 0.0)
    assert ladder["reversed"] == []


def test_a_resample_in_which_a_level_lost_nothing_is_left_out_of_every_pair():
    levels = (1000, 1100, 1200)
    samples = _ladder(levels=levels, per_band=20)
    # 1200 loses on one mover's two positions only: a resample without that mover has no ln for it.
    for band, sample in samples.items():
        sample.columns[1200] = [40.0 if band == 1100 and i < 2 else 0.0 for i in range(20)]

    ladder = probe.ladder_gaps(samples, levels, random.Random(1), resamples=400)

    usable = []
    for draw in probe.resample_sums(samples, 400, random.Random(1)):
        totals = {level: sum(sums[level] for _n, sums in draw.values()) for level in levels}
        if totals[1200] > 0:
            usable.append(math.log(totals[1100] / totals[1000]))
    assert 200 < len(usable) < 320  # about 35% of the resamples miss that mover
    assert ladder["resamples_unusable"] == 400 - len(usable)
    # The pair that never involves 1200 reads the same draws as the pairs that do.
    assert _gap(ladder, 1000, 1100)["s"] == pytest.approx(probe.stdev(usable))
    assert all(math.isfinite(g["s"]) and math.isfinite(g["bound"]) for g in ladder["gaps"])


@pytest.mark.parametrize(
    "columns",
    [
        # Each mover loses at one level only: the resamples that keep both levels all show the same gap.
        {1000: [0.0, 100.0], 1100: [200.0, 0.0]},
        # No zero anywhere: both movers show the same ratio, so no resample moves the gap.
        {1000: [100.0, 100.0], 1100: [200.0, 200.0]},
    ],
)
def test_the_ladder_gives_no_verdict_when_a_gap_has_no_measurable_uncertainty(columns):
    samples = {600: probe.BandSample(columns, [1, 2])}

    with pytest.raises(ValueError, match="uncertainty cannot be estimated"):
        probe.ladder_gaps(samples, [1000, 1100], random.Random(1), resamples=400)


def test_the_ladder_gives_no_verdict_from_a_single_resample():
    with pytest.raises(ValueError, match="uncertainty cannot be estimated"):
        probe.ladder_gaps(_ladder(levels=(1000, 1100)), [1000, 1100], random.Random(1), resamples=1)


# ---------------------------------------------------------------------------
# The flatness rule
# ---------------------------------------------------------------------------

RULE_BANDS = (600, 1100, 1300, 1500, 2000, 2300, 2600)


def _flatness(**changes) -> probe.FlatnessDecision:
    values = dict(
        beta_upper=-0.0004,
        d_curve=60.0,
        d_t1=200.0,
        share_flatter=0.99,
        e_curve_intervals={band: (-120.0, 90.0) for band in RULE_BANDS},
        variety_uppers={"natural 600": 2.4, "opening 600": 2.2, "opening 800": 3.0},
        reversed_pairs=[],
    )
    values.update(changes)
    return probe.decide_flatness(**values)


def test_the_curve_is_adopted_when_everything_holds():
    decision = _flatness()

    assert (decision.guard, decision.r1, decision.r2, decision.r3, decision.r4) == (True,) * 5
    assert decision.outcome == "all hold" and decision.recommendation.startswith("adopt")


def test_a_flat_human_fit_recommends_nothing():
    decision = _flatness(beta_upper=0.0001)

    assert not decision.guard and decision.recommendation.startswith("none")


@pytest.mark.parametrize("changes", [{"d_curve": 210.0}, {"share_flatter": 0.94}])
def test_r1_needs_a_flatter_curve_in_95_percent_of_resamples(changes):
    decision = _flatness(**changes)

    assert not decision.r1 and decision.outcome == "R1 fails"
    assert decision.recommendation.startswith("keep T=1")


@pytest.mark.parametrize(
    "interval, holds",
    [
        ((-400.0, -160.0), False),  # clearly weaker than the label
        ((160.0, 380.0), False),  # clearly stronger
        ((-400.0, -150.0), False),  # the upper end must be above -150
        ((-400.0, -149.0), True),
        ((-450.0, -50.0), True),  # an offset of -250 passes: a screen, not a certificate
    ],
)
def test_r2_fails_only_when_a_whole_interval_is_outside_150(interval, holds):
    intervals = {band: (-120.0, 90.0) for band in RULE_BANDS} | {2600: interval}
    decision = _flatness(e_curve_intervals=intervals)

    assert decision.r2 is holds
    assert decision.off_bands == (() if holds else (2600,))
    if not holds:
        assert decision.outcome == "only R2 fails" and "the user decides" in decision.recommendation


def test_r3_fails_only_when_a_whole_variety_interval_is_under_two():
    # A mean of 1.9 whose interval reaches 2.0 passes.
    reaches = _flatness(variety_uppers={"natural 600": 2.0, "opening 600": 2.2})
    # An opening-pool level that is not a band fails like any other.
    short = _flatness(variety_uppers={"natural 600": 2.4, "opening 800": 1.97})

    assert reaches.r3 and reaches.outcome == "all hold"
    assert not short.r3 and short.short_variety == ("opening 800",)
    assert short.outcome == "only R3 fails"


def test_a_reversal_on_the_ladder_stops_adoption():
    decision = _flatness(reversed_pairs=[[2400, 2600]])

    assert decision.r1 and decision.r2 and decision.r3 and not decision.r4
    assert decision.reversed_pairs == ((2400, 2600),)
    assert decision.outcome == "only R4 fails"
    assert "the user decides" in decision.recommendation and not decision.recommendation.startswith("adopt")
    # The follow-up must name the pool: --ladder alone reads the fit pool's positions.
    assert "--ladder --pool validation --temperature 1" in decision.recommendation
    follow_up = probe.parse_args(["--ladder", "--pool", "validation", "--temperature", "1"])
    assert (follow_up.pool, follow_up.temperature) == ("validation", 1.0)


def test_each_setting_on_a_pool_writes_its_own_ladder_file():
    """The T=1 ladder that follows a failed R4 must not replace the curve's ladder."""
    flat = probe.Setting(temperature=1.0)
    curve = probe.Setting(curve=((600, 1.2), (2600, 0.4)))
    revised = probe.Setting(curve=((600, 1.2), (2600, 0.5)))

    names = [probe.ladder_json_name("validation", "maia3-23m", setting) for setting in (flat, curve, revised)]

    assert names[0] == "ladder-validation-maia3-23m-T1.json"
    assert len(set(names)) == 3
    assert all(set(name) <= set("abcdefghijklmnopqrstuvwxyz0123456789T.-") for name in names)
    assert probe.ladder_json_name("fit", "maia3-23m", flat) != names[0]


def test_several_failed_screens_are_named_together():
    decision = _flatness(reversed_pairs=[[2400, 2600]], variety_uppers={"opening 800": 1.5})

    assert decision.outcome == "only R3, R4 fail"


# ---------------------------------------------------------------------------
# Held-out positions
# ---------------------------------------------------------------------------


def test_a_position_is_its_board_not_its_move_order():
    one = probe.Position("opening", tuple("e2e4 e7e5 g1f3 b8c6".split()))
    other = probe.Position("natural", tuple("g1f3 b8c6 e2e4 e7e5".split()), 600, "f1c4")
    # The same pieces with the other side to move.
    white = probe.Position("opening", tuple("g1f3 g8f6 f3g1 f6g8".split()))
    black = probe.Position("opening", tuple("g1f3 g8f6 f3g1 f6g8 g1f3".split()))
    again = probe.Position("opening", tuple("g1h3 g8f6 h3g1 f6g8 g1f3".split()))

    assert probe.position_id(one) == probe.position_id(other)
    assert probe.position_id(white) != probe.position_id(black)
    assert probe.position_id(black) == probe.position_id(again)


def test_assert_disjoint_names_a_shared_position():
    fit = [probe.Position("opening", tuple("e2e4 e7e5 g1f3 b8c6".split()))]
    validation = [
        probe.Position("natural", ("d2d4",), 600, "d7d5"),
        probe.Position("natural", tuple("g1f3 b8c6 e2e4 e7e5".split()), 600, "f1c4"),
    ]

    assert probe.assert_disjoint([("fit.json", fit), ("validation.json", validation[:1])]) == 2
    with pytest.raises(probe.FixtureError, match="1 positions are shared.*fit.json.*validation.json"):
        probe.assert_disjoint([("fit.json", fit), ("validation.json", validation)])


def test_a_build_skips_held_out_and_repeated_positions_and_still_fills_the_band():
    games = [_game(f"g{i}", plies=40) for i in range(8)]
    games.insert(2, games[1])  # the same game again: the same position
    held_out = probe.candidate_from_game(games[0], 1500, 1, 0)
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        build = probe.build_pool(
            games,
            judge_natural=lambda c: (True, []),
            judge_tactical=lambda c: (True, []),
            executor=executor,
            seed=1,
            per_band=4,
            tactical_per_band=0,
            bands=(1500,),
            exclude=frozenset({held_out.epd}),
        )

    # Game 0 is held out and game 2 repeats game 1: later games fill the band.
    assert [c.game_index for c in build.natural[1500]] == [1, 3, 4, 5]
    assert (build.dropped_excluded, build.dropped_repeated) == (1, 1)
    assert build.tactical == [] and build.only_move_windows == {}
    assert held_out.epd == probe.position_id(held_out.position("natural"))


def test_opening_lines_of_a_fit_fixture_leave_the_book_before_sampling():
    _header, fit = probe.load_fixture(check=False)
    fit_ids = frozenset(probe.position_id(p) for p in fit)
    validation_seed = 20261124

    plain = probe.opening_positions(probe.ECO_PATH, validation_seed)
    dropped: list[int] = []
    held_out = probe.opening_positions(probe.ECO_PATH, validation_seed, exclude=fit_ids, dropped=dropped)

    # Another seed alone is not enough: it draws lines the fit pool already has.
    assert sum(probe.position_id(p) in fit_ids for p in plain) >= 1
    assert len(held_out) == probe.OPENING_COUNT
    assert not any(probe.position_id(p) in fit_ids for p in held_out)
    assert dropped and dropped[0] >= probe.OPENING_COUNT


# ---------------------------------------------------------------------------
# Fixtures with movers: the builder, the sidecar, the loader
# ---------------------------------------------------------------------------


def _pgn(games: list[probe.PgnGame]) -> bytes:
    out = []
    for game in games:
        out += [f'[{key} "{value}"]' for key, value in game.headers.items()]
        out += ["", game.movetext, ""]
    return "\n".join(out).encode()


def _named(game: probe.PgnGame, white: str, black: str) -> probe.PgnGame:
    game.headers.update(White=white, Black=black)
    return game


class _FakeStockfish:
    """Stands in for StockfishWorkers: every position is playable and undecided."""

    name = "FakeFish 1"

    def __init__(self, path: str, workers: int) -> None:
        self.executor = concurrent.futures.ThreadPoolExecutor(2)

    def root(self, board: chess.Board, key: str) -> tuple[int, str]:
        return 15, min(move.uci() for move in board.legal_moves)

    def close(self) -> None:
        self.executor.shutdown()


def _build_args(tmp_path, pgn: bytes, *extra: str) -> list[str]:
    source = tmp_path / "games.pgn"
    source.write_bytes(pgn)
    return ["--build-pool", str(source), "--cache", str(tmp_path / "cache.sqlite3"), *extra]


@pytest.fixture
def small_builds(monkeypatch):
    monkeypatch.setattr(probe, "StockfishWorkers", _FakeStockfish)
    monkeypatch.setattr(probe, "stockfish_path", lambda explicit: "fake")
    monkeypatch.setattr(probe, "NATURAL_PER_BAND", 4)


def _two_band_stream() -> list[probe.PgnGame]:
    games = []
    for i in range(6):
        # "Anna" has both colours in her 1300 games, so she is to move in each.
        games.append(_named(_game(f"low{i}", plies=40, band_elo="1300"), "Anna" if i < 3 else f"w{i}", "Anna" if i < 3 else f"b{i}"))
        games.append(_named(_game(f"high{i}", plies=40, band_elo="2300"), f"W{i}", f"B{i}"))
    return games


def test_a_build_with_its_own_bands_and_no_tactical_pool_stores_mover_integers(
    tmp_path, small_builds, capsys
):
    out = tmp_path / "extra.json"
    stream = _two_band_stream()

    assert probe.main(_build_args(
        tmp_path, _pgn(stream), "--bands", "1300,2300", "--pools", "natural", "--fixture", str(out),
    )) == 0

    header, positions = probe.load_fixture(out)
    assert header["schema"] == 2 and probe.fixture_bands(header) == (1300, 2300)
    assert header["counts"] == {"natural": 8} and "tactical" not in header and "opening" not in header
    by_band = probe.natural_by_band(positions, (1300, 2300))
    assert {band: len(rows) for band, rows in by_band.items()} == {1300: 4, 2300: 4}
    # Anna's three positions share one integer; nobody's name is in the file.
    low = [p.mover for p in by_band[1300]]
    assert sorted(low.count(m) for m in set(low)) == [1, 3]
    assert header["natural_by_band"]["1300"]["movers"] == 2
    assert header["natural_by_band"]["2300"]["movers"] == 4
    text = out.read_text()
    assert not any(name in text for name in ("Anna", "w3", "W0", "B0"))
    assert "shared positions: 0" in capsys.readouterr().out
    # An existing fixture is never replaced by accident.
    with pytest.raises(SystemExit, match="--overwrite"):
        probe.main(_build_args(tmp_path, _pgn(stream), "--bands", "1300,2300", "--pools", "natural", "--fixture", str(out)))


def test_a_build_keeps_every_position_of_an_excluded_fixture_out(tmp_path, small_builds):
    stream = [_game(f"g{i}", plies=40) for i in range(12)]
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    common = ("--bands", "1500", "--pools", "natural,opening", "--seed", "5")

    probe.main(_build_args(tmp_path, _pgn(stream), *common, "--fixture", str(first)))
    probe.main(_build_args(
        tmp_path, _pgn(stream), *common, "--fixture", str(second), "--exclude-fixture", str(first),
    ))

    pool_a, pool_b = probe.load_pool(first), probe.load_pool(second)
    assert len(pool_b.natural()[1500]) == 4 and len(pool_b.opening()) == probe.OPENING_COUNT
    # The same stream and seed would give the same positions; the second build took later games.
    assert probe.assert_disjoint([(pool_a.name, pool_a.positions), (pool_b.name, pool_b.positions)]) == 88
    held = pool_b.header["held_out"]
    assert held["natural_candidates_dropped_as_excluded"] == 4
    assert held["opening_lines_dropped"] >= probe.OPENING_COUNT
    assert pool_b.header["excluded_fixtures"] == [
        {"file": "first.json", "sha256": pool_a.sha256, "positions": 44}
    ]


def _legacy_fixture(tmp_path, stream: list[probe.PgnGame], seed: int = 3) -> tuple[object, list[probe.Position]]:
    """A schema-1 fixture without movers, as the 2026-11-17 builder wrote it."""
    positions = []
    for index, game in enumerate(stream):
        candidate = probe.candidate_from_game(game, 1500, seed, index)
        positions.append(candidate.position("natural"))
    random.Random(4).shuffle(positions)
    path = tmp_path / "legacy.json"
    header = {"schema": 1, "seed": seed, "source_url": "test", "natural_filters": {"bands": [1500]}}
    probe.write_fixture(path, header, positions)
    return path, positions


def _legacy_stream() -> list[probe.PgnGame]:
    names = ["Anna", "Ben", "Anna", "Cleo", "Anna", "Dev"]
    return [_named(_game(f"s{i}", plies=40), name, name) for i, name in enumerate(names)]


def test_the_mover_sidecar_groups_a_players_positions_without_naming_anyone(tmp_path, capsys):
    stream = _legacy_stream()
    path, positions = _legacy_fixture(tmp_path, stream)
    source = tmp_path / "prefix.pgn"
    source.write_bytes(_pgn(stream))

    assert probe.main(["--mover-index", str(source), "--fixture", str(path)]) == 0

    sidecar = probe.movers_path(path)
    assert sidecar.name == "legacy_movers.json"
    assert not any(name in sidecar.read_text() for name in ("Anna", "Ben", "Cleo", "Dev"))
    pool = probe.load_pool(path)
    anna = {" ".join(probe.candidate_from_game(stream[i], 1500, 3, i).moves) for i in (0, 2, 4)}
    movers = {p.key: p.mover for p in pool.positions}
    assert len({movers[key] for key in anna}) == 1
    assert len(set(movers.values())) == 4
    assert probe.mover_summary(pool.positions)["movers"] == 4
    # The legacy loader ignores the sidecar, so the 2026-11-18 report still reproduces.
    assert all(p.mover is None for p in probe.load_fixture(path)[1])
    assert "1500" in capsys.readouterr().out


def test_the_mover_index_fails_when_a_position_has_no_game_in_the_stream(tmp_path):
    stream = _legacy_stream()
    _path, positions = _legacy_fixture(tmp_path, stream)

    with pytest.raises(probe.FixtureError, match="no game in the stream for 1 natural positions"):
        probe.mover_index(stream[:-1], positions, (1500,), seed=3)
    # Another seed draws other plies: nothing matches.
    with pytest.raises(probe.FixtureError):
        probe.mover_index(stream, positions, (1500,), seed=99)


def test_a_sidecar_of_another_fixture_is_refused(tmp_path):
    stream = _legacy_stream()
    path, positions = _legacy_fixture(tmp_path, stream)
    probe.write_movers(path, {"source_url": "test"}, probe.mover_index(stream, positions, (1500,), seed=3))
    assert probe.load_pool(path).positions[0].mover is not None

    path.write_text(path.read_text() + "\n")  # any other bytes: another sha256

    with pytest.raises(probe.FixtureError, match="was not built for this"):
        probe.load_pool(path)


def test_the_loader_reads_both_schemas(tmp_path):
    with_mover = probe.Position("natural", ("e2e4",), 1300, "e7e5", mover=4)
    without = probe.Position("natural", ("d2d4",), 1300, "d7d5")
    filters = {"natural_filters": {"bands": [1300]}}
    old, new, future = tmp_path / "old.json", tmp_path / "new.json", tmp_path / "future.json"
    probe.write_fixture(old, {"schema": 1, **filters}, [without])
    probe.write_fixture(new, {"schema": 2, **filters}, [with_mover, without])
    probe.write_fixture(future, {"schema": 3, **filters}, [without])

    assert probe.load_fixture(old)[1] == [without]
    assert probe.load_fixture(new)[1] == [with_mover, without]
    with pytest.raises(probe.FixtureError):
        probe.load_fixture(future)
    # A band that the fixture's header does not name is refused.
    probe.write_fixture(future, {"schema": 2, "natural_filters": {"bands": [600]}}, [without])
    with pytest.raises(probe.FixtureError, match="unknown band"):
        probe.load_fixture(future)
    with pytest.raises(probe.FixtureError, match="mover"):
        probe.position_from_json({"pool": "opening", "moves": "e2e4", "mover": 1}, (1300,))


def test_validation_refuses_a_fit_fixture_it_was_not_built_against(tmp_path):
    def write(name: str, band: int, first: str, **header) -> object:
        path = tmp_path / name
        position = probe.Position("natural", (first,), band, "g8f6", mover=0)
        probe.write_fixture(path, {"schema": 2, "natural_filters": {"bands": [band]}, **header}, [position])
        return path

    main = write("main.json", 1500, "e2e4")
    extra = write("extra.json", 1300, "d2d4")
    recorded = [
        {"file": path.name, "sha256": probe.fixture_digest(path), "positions": 1} for path in (main, extra)
    ]
    validation = write("validation.json", 1500, "c2c4", excluded_fixtures=recorded)
    args = argparse.Namespace(fixture=main, fit_extra=extra, validation_fixture=validation)

    natural, opening, pools = probe.validation_pools(args)
    assert list(natural) == [1500] and opening == [] and len(pools) == 3
    fit_natural, _opening, _pools = probe.fit_pools(args)
    assert list(fit_natural) == [1300, 1500]

    # The fit fixture changed after the validation pool was built.
    write("extra.json", 1300, "g1f3")
    with pytest.raises(probe.FixtureError, match="was not built with extra.json"):
        probe.validation_pools(args)
    # A validation pool that holds a fit position.
    write("extra.json", 1300, "d2d4")
    shared = write("validation.json", 1500, "e2e4", excluded_fixtures=recorded)
    with pytest.raises(probe.FixtureError, match="shared between fixtures"):
        probe.validation_pools(argparse.Namespace(fixture=main, fit_extra=extra, validation_fixture=shared))


# ---------------------------------------------------------------------------
# The fit and the flatness check, end to end on a synthetic engine
# ---------------------------------------------------------------------------

BETA = -0.0006


class _SyntheticEngine:
    """Stands in for the cached results.

    A human of band b loses base * exp(BETA * b) on a position. The engine at
    Elo e and temperature T plays like a human rated e + 300 - 500 * T, so it
    matches its label at T = 0.6 at every Elo, is 300 too strong at T = 0 and
    200 too weak at T = 1. Its variety is 1 + ``variety_slope`` * T distinct
    moves on average.
    """

    def __init__(self, variety_slope: float = 6.0) -> None:
        self.variety_slope = variety_slope

    @staticmethod
    def _temperature(setting: str, elo: int) -> float:
        parsed = probe.Setting.from_name(setting)
        if parsed.curve is not None:
            return probe.run_settings("maia3-23m", parsed, 1, ".").temperature_for(elo)
        return parsed.temperature * parsed.top_p  # a TopP arm samples a little colder

    @staticmethod
    def _base(position) -> float:
        return 20 + 80 * random.Random(position.key).random()

    def loss(self, position, move) -> float:
        return self._base(position) * math.exp(BETA * position.band)

    def losses(self, position, setting: str, elo: int, k: int) -> list[float]:
        rated = elo + 300 - 500 * self._temperature(setting, elo)
        return [
            self._base(position) * math.exp(BETA * rated)
            # Sampling noise with mean 1.
            * random.Random(f"{position.key}:{setting}:{elo}:{sample}").lognormvariate(-0.005, 0.1)
            for sample in range(k)
        ]

    def distinct(self, position, setting: str, elo: int, k: int) -> int:
        phase = random.Random(f"v:{position.key}").random()
        return 1 + math.floor(self.variety_slope * self._temperature(setting, elo) + phase)


def _synthetic_pools(per_band: int = 120) -> tuple[dict, list]:
    natural = {
        band: [
            probe.Position("natural", (f"n{band}", str(i)), band, "human", mover=i // 2)
            for i in range(per_band)
        ]
        for band in RULE_BANDS
    }
    opening = [probe.Position("opening", (f"o{i}",), label=str(i)) for i in range(40)]
    return natural, opening


@pytest.fixture
def small_statistics(monkeypatch):
    monkeypatch.setattr(probe, "H1_PER_BAND", 40)
    monkeypatch.setattr(probe, "LADDER_PER_BAND", 60)
    monkeypatch.setattr(probe, "BOOTSTRAP_RESAMPLES", 200)
    monkeypatch.setattr(probe, "LADDER_RESAMPLES", 400)


def test_the_fit_finds_the_temperature_at_which_the_engine_matches_its_label(small_statistics, capsys):
    natural, opening = _synthetic_pools()

    report = probe.build_fit(_SyntheticEngine(), natural, opening)

    assert report["beta"] == pytest.approx(BETA, rel=0.1)
    for band, fit in report["bands"].items():
        assert fit["t_star"] == pytest.approx(0.6, abs=0.05)
        low, high = fit["t_star_spread"]["interval"]
        assert low <= fit["t_star"] <= high and high - low < 0.3
        assert fit["t_star_spread"]["se_movers"] > 0 and fit["t_star_spread"]["se_positions"] > 0
        assert (fit["anchor"], fit["set_by"]) == (round(fit["t_star"], 2), "crossing")
        # T=0 is one move per position, so it is the noisiest column.
        assert fit["e"][0] == pytest.approx(300, abs=70) and fit["e"][-1] == pytest.approx(-200, abs=40)
        assert fit["floor"]["floor"] == 0.2 and fit["floor"]["below_grid"]
        assert fit["movers"]["movers"] == 60
        assert ("top_p_arms" in fit) is (band in probe.TOPP_BANDS)
    anchors = probe.parse_temperature_curve(report["curve"])
    assert [elo for elo, _t in anchors] == list(RULE_BANDS)
    # The printed report ends with the curve the next steps take.
    report.update(model="synthetic", engine="none", fixtures={"a.json": "0" * 64})
    probe.print_fit(report)
    assert capsys.readouterr().out.rstrip().endswith(f"curve: {report['curve']}")


def test_a_binding_variety_floor_sets_the_anchor_and_says_so(small_statistics):
    natural, opening = _synthetic_pools()

    # Variety reaches 2.0 only at T = 2/3, above the 0.6 where the CPL matches.
    report = probe.build_fit(_SyntheticEngine(variety_slope=1.5), natural, opening)

    for fit in report["bands"].values():
        assert fit["floor"]["floor"] == pytest.approx(2 / 3, abs=0.06)
        assert fit["set_by"] == "floor" and fit["anchor"] >= fit["floor"]["floor"]
        assert fit["anchor"] > round(fit["t_star"], 2)


def test_the_rule_adopts_a_curve_that_matches_the_labels(small_statistics, capsys):
    natural, opening = _synthetic_pools()
    curve = probe.Setting(curve=tuple((band, 0.6) for band in RULE_BANDS))

    report = probe.build_validation(_SyntheticEngine(), natural, opening, curve)

    decision = report["decision"]
    assert decision["outcome"] == "all hold" and decision["recommendation"].startswith("adopt")
    assert report["d"]["curve"] < 40 < 160 < report["d"]["t1"]
    assert report["share_flatter"] == 1.0
    low, high = report["improvement"]["interval"]
    assert low > 100 and low <= report["improvement"]["value"] <= high
    for band, row in report["bands"].items():
        assert row["e_curve"]["value"] == pytest.approx(0, abs=40)
        assert row["e_t1"]["value"] == pytest.approx(-200, abs=45)
        assert row["e_t0"]["value"] == pytest.approx(300, abs=70)
        assert row["e_curve"]["se_movers"] > 0 and row["e_curve"]["se_positions"] > 0
    # Variety is reported on each band's natural positions and on the opening pool at every level.
    assert {f"natural {band}" for band in RULE_BANDS} <= set(report["variety"])
    assert {f"opening {level}" for level in probe.ELO_BINS} | {"opening 2300"} <= set(report["variety"])
    assert report["ladder"]["positions"] == 7 * 60 and len(report["ladder"]["gaps"]) == 120
    report.update(model="synthetic", engine="none", fixtures={"v.json": "0" * 64})
    probe.print_validation(report)
    out = capsys.readouterr().out
    assert "outcome: all hold" in out and "clear reversals: none" in out
    # Decision 5: the text shows each standard error both ways, not only the interval by movers.
    lines = out.splitlines()
    for start, row in (
        (f"natural {RULE_BANDS[0]}", report["variety"][f"natural {RULE_BANDS[0]}"]),
        (f"opening {probe.ELO_BINS[0]}", report["variety"][f"opening {probe.ELO_BINS[0]}"]),
    ):
        line = next(text for text in lines if text.startswith(start + " "))
        assert f"{row['se_movers']:.2f}/{row['se_positions']:.2f}" in line
    for start in ("fit ln H", "improvement D_1 - D_c"):
        line = next(text for text in lines if text.startswith(start))
        assert "by movers" in line and "by positions" in line


def test_the_rule_does_not_adopt_a_curve_with_a_reversal_or_one_that_repeats_itself(small_statistics):
    natural, opening = _synthetic_pools()
    # 2400 plays the top move and 2600 samples at T=1: 2600 ends up about 300 Elo below 2400.
    reversed_curve = probe.Setting(curve=((600, 0.6), (2300, 0.6), (2400, 0.0), (2600, 1.0)))

    report = probe.build_validation(_SyntheticEngine(), natural, opening, reversed_curve)

    decision = report["decision"]
    assert not decision["r4"] and (2400, 2600) in decision["reversed_pairs"]
    assert not decision["recommendation"].startswith("adopt")
    # At T=0 a level plays one move 20 times out of 20: R3 names it.
    assert "opening 2400" in decision["short_variety"]
    assert report["variety"]["opening 2400"]["value"] == 1.0


def test_a_curve_that_is_no_flatter_than_temperature_1_keeps_temperature_1(small_statistics):
    natural, opening = _synthetic_pools()

    report = probe.build_validation(
        _SyntheticEngine(), natural, opening, probe.Setting(curve=((600, 1.0),))
    )

    assert not report["decision"]["r1"] and report["decision"]["outcome"] == "R1 fails"
    assert report["improvement"]["value"] == pytest.approx(0, abs=30)


# ---------------------------------------------------------------------------
# The committed fit and validation fixtures
# ---------------------------------------------------------------------------

SEVEN_BANDS = (600, 1100, 1300, 1500, 2000, 2300, 2600)


@pytest.fixture(scope="module")
def committed_pools() -> dict[str, probe.Pool]:
    """The three fixtures as the runs read them (every move checked for legality)."""
    return {
        "fit": probe.load_pool(probe.FIXTURE_PATH),
        "extra": probe.load_pool(probe.FIT_EXTRA_PATH),
        "validation": probe.load_pool(probe.VALIDATION_PATH),
    }


def test_the_committed_mover_sidecar_covers_every_natural_position(committed_pools):
    pool = committed_pools["fit"]

    assert pool.header["schema"] == 1  # the 2026-11-17 file itself is untouched
    for band, rows in pool.natural().items():
        assert len(rows) == probe.NATURAL_PER_BAND
        summary = probe.mover_summary(rows)
        assert summary is not None and 1 < summary["movers"] <= probe.NATURAL_PER_BAND
    assert all(p.mover is None for p in pool.positions if p.pool != "natural")


@pytest.mark.parametrize(
    "name, bands, counts",
    [
        ("extra", (1300, 2300), {"natural": 2000}),
        ("validation", SEVEN_BANDS, {"natural": 7000, "opening": probe.OPENING_COUNT}),
    ],
)
def test_the_new_fixtures_have_their_pools_movers_and_provenance(committed_pools, name, bands, counts):
    pool = committed_pools[name]
    header = pool.header

    assert header["schema"] == 2 and pool.bands == bands and header["counts"] == counts
    for key in ("source_url", "games_scanned", "stream_utc_range", "seed", "stockfish", "held_out"):
        assert header[key], key
    for band, rows in pool.natural().items():
        assert len(rows) == probe.NATURAL_PER_BAND
        window = header["natural_by_band"][str(band)]
        assert window["candidates_judged"] - window["rejected_decided"] == probe.NATURAL_PER_BAND
        assert window["movers"] == probe.mover_summary(rows)["movers"]
        assert all(probe.PLY_RANGE[0] <= len(p.moves) <= probe.PLY_RANGE[1] for p in rows)
    assert len(pool.opening()) == counts.get("opening", 0)
    # No position twice inside a fixture.
    assert len({probe.position_id(p) for p in pool.positions}) == len(pool.positions)
    # It records, by sha256, every fixture whose positions it was built to avoid.
    recorded = {entry["file"]: entry["sha256"] for entry in header["excluded_fixtures"]}
    expected = ["fit"] if name == "extra" else ["fit", "extra"]
    assert recorded == {committed_pools[k].name: committed_pools[k].sha256 for k in expected}


def test_the_validation_pool_is_another_month_and_seed(committed_pools):
    fit, extra, validation = (committed_pools[k].header for k in ("fit", "extra", "validation"))

    assert fit["source_url"] == extra["source_url"] != validation["source_url"]
    assert fit["seed"] == extra["seed"] != validation["seed"]


def test_no_position_is_shared_between_the_fit_and_validation_fixtures(committed_pools):
    args = argparse.Namespace(
        fixture=probe.FIXTURE_PATH, fit_extra=probe.FIT_EXTRA_PATH, validation_fixture=probe.VALIDATION_PATH
    )

    # What --validate runs before any engine starts: it raises on a shared
    # position, between the two fit fixtures too, and on a changed fit fixture.
    natural, opening, pools = probe.validation_pools(args)

    assert tuple(natural) == SEVEN_BANDS and len(opening) == probe.OPENING_COUNT
    assert [p.name for p in pools] == [committed_pools[k].name for k in ("validation", "fit", "extra")]
    assert sum(len(p.positions) for p in pools) == 5070 + 2000 + 7040
    # The fit pool is the seven bands of the two fit fixtures and the first one's openings.
    fit, extra = committed_pools["fit"], committed_pools["extra"]
    assert sorted(set(fit.bands) | set(extra.bands)) == list(SEVEN_BANDS)
    assert not set(fit.bands) & set(extra.bands) and len(fit.opening()) == probe.OPENING_COUNT


def test_a_resample_without_any_loss_in_a_band_is_left_out_and_counted(small_statistics):
    class _MostlyPerfectHumans(_SyntheticEngine):
        def loss(self, position, move) -> float:
            # At 2600 only the first position's human loses anything.
            if position.band == 2600 and position.moves[1] != "0":
                return 0.0
            return super().loss(position, move)

    natural, opening = _synthetic_pools(per_band=60)
    curve = probe.Setting(curve=tuple((band, 0.6) for band in RULE_BANDS))

    report = probe.build_validation(_MostlyPerfectHumans(), natural, opening, curve)

    # About a third of the resamples miss that one mover; the rest still give intervals.
    assert 20 < report["resamples_unusable"] < 120
    low, high = report["bands"][2600]["e_curve"]["interval"]
    assert math.isfinite(low) and math.isfinite(high)


def test_an_offset_has_no_value_when_the_human_slope_is_exactly_zero():
    bands = (600, 1100, 1500)

    offsets = probe.band_offsets(bands, {b: 50.0 for b in bands}, {"T1": {b: 40.0 for b in bands}})

    assert offsets["beta"] == 0
    assert all(math.isnan(e) for e in offsets["e"]["T1"].values())
    assert math.isnan(probe.mean_abs_offset(offsets["e"]["T1"], bands))


def test_a_human_slope_of_exactly_zero_is_an_unusable_fit_not_a_crash(small_statistics, capsys):
    class _FlatHumans(_SyntheticEngine):
        def loss(self, position, move) -> float:
            return 50.0  # the same at every band: ln H has no slope at all

    natural, opening = _synthetic_pools(per_band=60)
    curve = probe.Setting(curve=tuple((band, 0.6) for band in RULE_BANDS))

    report = probe.build_validation(_FlatHumans(), natural, opening, curve)

    assert report["beta"]["value"] == 0 and report["beta"]["interval"] == (0, 0)
    decision = report["decision"]
    assert not decision["guard"] and decision["outcome"] == "the human fit is unusable"
    assert decision["recommendation"].startswith("none")
    for row in report["bands"].values():
        assert math.isnan(row["e_curve"]["value"]) and math.isnan(row["e_curve"]["interval"][0])
    # The ladder's verdict does not use the slope; only its sizes in Elo do.
    assert report["ladder"]["reversed"] == []
    report.update(model="synthetic", engine="none", fixtures={"v.json": "0" * 64})
    probe.print_validation(report)
    out = capsys.readouterr().out
    assert "outcome: the human fit is unusable" in out and "no size in Elo" in out

    fit = probe.build_fit(_FlatHumans(), natural, opening)
    fit.update(model="synthetic", engine="none", fixtures={"a.json": "0" * 64})
    probe.print_fit(fit)  # the E column has no value; the anchors do not depend on it
    assert all(math.isnan(e) for band in fit["bands"].values() for e in band["e"])


# ---------------------------------------------------------------------------
# Blitz against rapid and classical (--speed-counts, decision 7)
# ---------------------------------------------------------------------------


def _analysed(evals: dict[int, str | None] | None = None, plies: int = 20, flat: str | None = "0.30",
              clocks: dict[int, float] | None = None) -> str:
    """``plies`` moves, each with a [%clk] and White's [%eval] after it (move index -> override)."""
    parts = []
    for i in range(plies):
        number = f"{i // 2 + 1}." if i % 2 == 0 else f"{i // 2 + 1}..."
        value = (evals or {}).get(i, flat)
        clock = (clocks or {}).get(i, 120.0)
        comment = ("" if value is None else f"[%eval {value}] ") + f"[%clk 0:{int(clock) // 60:02d}:{int(clock) % 60:02d}]"
        parts.append(f"{number} Nf3 {{ {comment} }}")
    return " ".join(parts) + " 1-0"


def _after_a_loss(cp: int) -> dict[int, str]:
    """White loses ``cp`` at move index 16 and nothing changes afterwards (20 plies: four counted moves)."""
    return dict.fromkeys(range(16, 20), f"{0.30 - cp / 100:.2f}")


def _speed_game(time_control: str, elo: int = 1500, loss_cp: int | None = None, **changes) -> probe.PgnGame:
    headers = _headers(TimeControl=time_control, WhiteElo=str(elo), BlackElo=str(elo), **changes)
    return probe.PgnGame(headers, _analysed(_after_a_loss(loss_cp)) if loss_cp is not None else _analysed(flat=None))


@pytest.mark.parametrize(
    "seconds, speed",
    [(None, None), (29, None), (30, "bullet"), (179, "bullet"), (180, "blitz"), (479, "blitz"),
     (480, "rapid"), (1499, "rapid"), (1500, "classical"), (10800, "classical")],
)
def test_a_speed_is_lichess_s_own_by_estimated_duration(seconds, speed):
    assert probe.speed_of(seconds) == speed
    # The pool's blitz filter and the count's blitz are the same games.
    if seconds is not None:
        assert (probe.qualify(_headers(TimeControl=f"{seconds}+0")) is not None) is (speed == "blitz")


def test_a_server_eval_loss_is_the_movers_drop_in_centipawns():
    # Move 16 (White): +0.30 -> -1.20. Move 17 (Black): unchanged. Move 18 (White): -1.20 -> -0.20, a gain.
    # Move 19 (Black): -0.20 -> +0.80.
    losses = probe.server_eval_losses(_analysed({16: "-1.20", 17: "-1.20", 18: "-0.20", 19: "0.80"}))

    assert losses == pytest.approx([150.0, 0.0, 0.0, 100.0])


def test_server_eval_losses_cover_the_pools_plies_only():
    assert len(probe.server_eval_losses(_analysed(plies=16))) == 0  # the mover never moved from ply 16
    assert len(probe.server_eval_losses(_analysed(plies=17))) == 1
    low, high = probe.PLY_RANGE
    assert len(probe.server_eval_losses(_analysed(plies=90))) == high - low + 1


@pytest.mark.parametrize(
    "evals, clocks, expected",
    [
        # White had 20 s left, so move 16 is out; Black's move 17 gives the 150 back and counts.
        ({16: "-1.20"}, {14: 20.0}, [150.0, 0.0, 0.0]),
        ({15: "12.50", 16: "0.30"}, None, [0.0, 0.0, 0.0]),  # decided before move 16
        ({15: "#3", 16: "0.30"}, None, [0.0, 0.0, 0.0]),  # a mate on the board before move 16
        ({15: "2.00", 16: "#-3", 17: "#-3", 18: "#-3", 19: "#-3"}, None, [1000.0]),  # walks into a mate: capped
        ({16: None}, None, [0.0, 0.0]),  # no eval after move 16: 16 and 17 have no difference
    ],
)
def test_server_eval_losses_follow_the_pools_position_rules(evals, clocks, expected):
    losses = probe.server_eval_losses(_analysed(evals, clocks=clocks))

    assert losses == pytest.approx(expected)


def _speed_games() -> list[probe.PgnGame]:
    return [
        _speed_game("180+2", loss_cp=240),  # blitz: per-game mean 60
        _speed_game("300+0", loss_cp=160),  # blitz: 40
        _speed_game("180+0"),  # blitz, not analysed
        _speed_game("600+0", loss_cp=80),  # rapid: 20
        _speed_game("900+10", loss_cp=120),  # rapid: 30
        _speed_game("1800+0", loss_cp=40),  # classical: one analysed game
        _speed_game("60+0", loss_cp=400),  # bullet
        _speed_game("600+0", loss_cp=80, WhiteTitle="BOT"),  # rated rapid, but not a qualifying game
        _speed_game("600+0", elo=1750, loss_cp=80),  # rated rapid, in no band
        _speed_game("600+0", loss_cp=80, Event="Casual Rapid game"),
        _speed_game("15+0", loss_cp=80),  # ultrabullet
    ]


def test_speed_counts_compare_rapid_with_blitz_at_the_same_rating_number():
    report = probe.speed_counts(_speed_games(), (1500, 2000), slope=-0.0005)

    assert report["games_read"] == 11
    assert report["rated_standard"] == {"bullet": 1, "blitz": 3, "rapid": 4, "classical": 1}
    row = report["bands"][1500]
    assert {speed: row[speed]["qualifying"] for speed in probe.SPEEDS} == {
        "bullet": 1, "blitz": 3, "rapid": 2, "classical": 1,
    }
    assert (row["blitz"]["analysed"], row["rapid"]["analysed"], row["classical"]["analysed"]) == (2, 2, 1)
    assert row["blitz"]["mean_cpl"] == pytest.approx(50) and row["blitz"]["se"] == pytest.approx(10)
    assert row["rapid"]["mean_cpl"] == pytest.approx(25) and row["rapid"]["se"] == pytest.approx(5)
    assert row["blitz"]["share_ge_200"] == pytest.approx(1 / 8) and row["rapid"]["share_ge_200"] == 0
    versus = row["rapid"]["vs_blitz"]
    assert versus["log_ratio"] == pytest.approx(math.log(0.5))
    assert versus["se"] == pytest.approx(math.hypot(0.2, 0.2))
    # Rapid players lose half as much: over a slope of -0.0005 they are ln 2 / 0.0005 Elo stronger.
    assert versus["elo"] == pytest.approx(math.log(2) / 0.0005)
    assert versus["elo_se"] == pytest.approx(math.hypot(0.2, 0.2) / 0.0005)
    assert "vs_blitz" not in row["classical"]  # one analysed game has no standard error
    assert all(cell == {"qualifying": 0, "analysed": 0} for cell in report["bands"][2000].values())


def test_speed_counts_stop_at_max_games_and_need_a_slope_for_elo():
    assert probe.speed_counts(_speed_games(), (1500,), max_games=2)["games_read"] == 2
    versus = probe.speed_counts(_speed_games(), (1500,))["bands"][1500]["rapid"]["vs_blitz"]
    assert "elo" not in versus and versus["log_ratio"] == pytest.approx(math.log(0.5))


def test_the_speed_count_mode_reads_a_pgn_file_and_prints_the_comparison(tmp_path, capsys):
    pgn = tmp_path / "games.pgn"
    pgn.write_text("".join(
        "".join(f'[{key} "{value}"]\n' for key, value in game.headers.items()) + f"\n{game.movetext}\n\n"
        for game in _speed_games()
    ))
    out_json = tmp_path / "speeds.json"

    assert probe.main(["--speed-counts", str(pgn), "--bands", "1500,2000", "--slope", "-0.0005", "--json", str(out_json)]) == 0

    out = capsys.readouterr().out
    assert "11 games read" in out
    line = next(text for text in out.splitlines() if text.startswith(" 1500") and "Elo" in text)
    assert "rapid: -50.0%" in line and "+1386 Elo" in line and "classical: not measured" in line
    stored = json.loads(out_json.read_text())
    assert stored["bands"]["1500"]["rapid"]["qualifying"] == 2 and stored["slope"] == -0.0005
