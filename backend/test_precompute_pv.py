"""PV/classification/terminal/ordering + engine-liveness tests for precompute.

Drives the analyzer through a fake-Stockfish subprocess stub that emits canned
UCI lines, so no real engine or full precompute run is required.
"""
import importlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# Fool's-mate position: white just played g4, black to move; d8h4 is checkmate.
FOOLS_MATE_FEN = "rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq g3 0 2"


def _load_script():
    scripts_dir = Path(__file__).resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("precompute_openings")


_STUB = '''#!{python}
import sys, os, json, time
log = open(os.environ["STUB_LOG"], "a")
mode = os.environ.get("STUB_MODE", "normal")
responses = []
if os.environ.get("STUB_RESPONSES"):
    responses = json.load(open(os.environ["STUB_RESPONSES"]))
ri = 0
opts = [
    "id name Stockfish 18",
    "option name EvalFile type string default nn-c288c895ea92.nnue",
    "option name EvalFileSmall type string default nn-37f18f62d772.nnue",
]
for line in sys.stdin:
    cmd = line.strip()
    log.write(cmd + "\\n"); log.flush()
    if cmd == "uci":
        if mode == "exit_on_uci":
            sys.exit(0)
        for o in opts:
            print(o, flush=True)
        print("uciok", flush=True)
    elif cmd == "isready":
        print("readyok", flush=True)
    elif cmd.startswith("go"):
        if mode == "hang":
            time.sleep(60)
        resp = responses[ri]; ri += 1
        for info in resp.get("info", []):
            print(info, flush=True)
        print("bestmove " + resp.get("bestmove", "(none)"), flush=True)
    elif cmd == "quit":
        break
'''


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """Write an executable fake-Stockfish stub and return a configurator."""
    log_path = tmp_path / "stub.log"
    resp_path = tmp_path / "responses.json"
    stub_path = tmp_path / "fake_stockfish"
    stub_path.write_text(_STUB.format(python=sys.executable))
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("STUB_LOG", str(log_path))

    def configure(responses=None, mode="normal"):
        monkeypatch.setenv("STUB_MODE", mode)
        if responses is not None:
            resp_path.write_text(json.dumps(responses))
            monkeypatch.setenv("STUB_RESPONSES", str(resp_path))
        return str(stub_path)

    configure.log_path = log_path
    return configure


def _commands(log_path):
    return [l for l in log_path.read_text().splitlines() if l]


def test_normal_position_pv_classification_and_ordering(stub):
    mod = _load_script()
    responses = [
        # 1. root/best search (no moves) — runs FIRST.
        {"info": ["info depth 24 score cp 25 pv e2e4 e7e5 g1f3"], "bestmove": "e2e4"},
        # 2. played search (after a2a3).
        {"info": ["info depth 24 score cp -15 pv e7e5 d2d4"], "bestmove": "e7e5"},
        # 3. post-best search (after e2e4).
        {"info": ["info depth 24 score cp 35 pv e7e5 g1f3"], "bestmove": "e7e5"},
    ]
    path = stub(responses)
    pos = mod.PositionToAnalyze(fen_before=START_FEN, move_uci="a2a3", move_san="a3")
    result, outcome = mod.analyze_position(pos, 24, path)

    assert outcome == mod.OK
    assert result.best_move_uci == "e2e4"
    # Root PV starts with the best move, so it is used verbatim.
    assert result.best_line_uci == "e2e4 e7e5 g1f3"
    assert result.best_line_uci.split()[0] == result.best_move_uci

    # Storage conversion: white-relative cp (post-move side is black).
    assert result.played_eval == mod._raw_to_white_cp({"type": "cp", "value": -15}, False)
    assert result.best_eval == mod._raw_to_white_cp({"type": "cp", "value": 35}, False)
    assert result.eval_delta == max(result.best_eval - result.played_eval, 0)

    from app.move_classification import EngineScore, classify_move_advanced
    expected = classify_move_advanced(
        EngineScore(type="cp", value=35),   # post-best (prev)
        EngineScore(type="cp", value=-15),  # post-played (next)
        score_pov="black", mover="white", is_best=False,
    )
    assert result.classification == expected

    cmds = _commands(stub.log_path)
    # ucinewgame issued exactly once for the position (not per search).
    assert cmds.count("ucinewgame") == 1
    # Root search (no "moves") issued BEFORE the played search.
    gos = [c for c in cmds if c.startswith("position fen")]
    assert " moves " not in gos[0]
    assert gos[1].endswith("moves a2a3")


def test_mate_score_parsed_and_stored(stub):
    mod = _load_script()
    responses = [
        {"info": ["info depth 24 score cp 10 pv e2e4 e7e5 g1f3"], "bestmove": "e2e4"},
        {"info": ["info depth 24 score mate -2 pv e7e5 d1h5"], "bestmove": "e7e5"},
        {"info": ["info depth 24 score cp 20 pv e7e5 g1f3"], "bestmove": "e7e5"},
    ]
    path = stub(responses)
    pos = mod.PositionToAnalyze(fen_before=START_FEN, move_uci="a2a3", move_san="a3")
    result, outcome = mod.analyze_position(pos, 24, path)
    assert outcome == mod.OK
    # Played move walked into mate-in-2 for the side to move (black); stored
    # white-relative mate count.
    assert result.played_eval_mate == mod._white_mate({"type": "mate", "value": -2}, False)
    assert result.best_eval_mate is None


def test_mate_in_one_terminal_no_search_and_skipped(stub):
    mod = _load_script()
    # Root search returns the mating move with a single-move PV; played == best,
    # so the played eval is synthesized (terminal) with NO extra search.
    responses = [
        {"info": ["info depth 1 score mate 1 pv d8h4"], "bestmove": "d8h4"},
    ]
    path = stub(responses)
    pos = mod.PositionToAnalyze(fen_before=FOOLS_MATE_FEN, move_uci="d8h4", move_san="Qh4#")
    result, outcome = mod.analyze_position(pos, 24, path)

    # Single-move PV with no continuation -> intentionally not cached.
    assert outcome == mod.SKIPPED_NO_CONTINUATION
    cmds = _commands(stub.log_path)
    # Exactly one search: the root. No search for the terminal played position.
    assert sum(1 for c in cmds if c.startswith("go")) == 1
    assert cmds.count("ucinewgame") == 1


def test_engine_hang_raises_timeout(stub, monkeypatch):
    mod = _load_script()
    monkeypatch.setattr(mod, "SEARCH_DEADLINE_S", 0.8)
    path = stub(responses=[], mode="hang")
    pos = mod.PositionToAnalyze(fen_before=START_FEN, move_uci="a2a3", move_san="a3")
    with pytest.raises(mod.EngineTimeout):
        mod.analyze_position(pos, 24, path)


def test_engine_early_exit_raises_died(stub, monkeypatch):
    mod = _load_script()
    monkeypatch.setattr(mod, "HANDSHAKE_DEADLINE_S", 2.0)
    path = stub(responses=[], mode="exit_on_uci")
    pos = mod.PositionToAnalyze(fen_before=START_FEN, move_uci="a2a3", move_san="a3")
    with pytest.raises(mod.EngineDied):
        mod.analyze_position(pos, 24, path)


def test_verify_stored_gates_on_authoritative_v2(tmp_path):
    """_verify_stored accepts a proper v2 canonical row and flags incomplete ones."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import AnalysisCache, Base
    from app.analysis_profiles import (
        CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile,
    )
    from app.evidence_contracts import RESOLVER_COMPLETE_V2

    mod = _load_script()
    engine = create_engine(f"sqlite:///{tmp_path/'v.db'}")
    Base.metadata.create_all(engine)
    p = get_profile(CANONICAL_PROFILE_ID)

    good = mod.AnalysisResult(
        fen_before=START_FEN, move_uci="a2a3", move_san="a3",
        best_move_uci="e2e4", best_move_san="e4", best_line_uci="e2e4 e7e5",
        played_eval=0, played_eval_mate=None, best_eval=30, best_eval_mate=None,
        eval_delta=30, classification="good",
    )
    bad = mod.AnalysisResult(
        fen_before=START_FEN, move_uci="b2b3", move_san="b3",
        best_move_uci="e2e4", best_move_san="e4", best_line_uci="e2e4 e7e5",
        played_eval=0, played_eval_mate=None, best_eval=30, best_eval_mate=None,
        eval_delta=30, classification="good",
    )
    with Session(engine) as db:
        db.add(AnalysisCache(
            fen_before=good.fen_before, move_uci=good.move_uci, move_san="a3",
            best_move_uci="e2e4", best_move_san="e4", best_line_uci="e2e4 e7e5",
            classification="good",
            played_eval=0, best_eval=30, eval_delta=30, source="precomputed",
            analysis_profile_id=CANONICAL_PROFILE_ID,
            evidence_contract_id=RESOLVER_COMPLETE_V2,
            **{f: getattr(p, f) for f in IDENTITY_FIELDS},
        ))
        # 'bad' row is stored but missing classification -> must be flagged.
        db.add(AnalysisCache(
            fen_before=bad.fen_before, move_uci=bad.move_uci, move_san="b3",
            best_move_uci="e2e4", best_line_uci="e2e4 e7e5", classification=None,
            played_eval=0, best_eval=30, eval_delta=30, source="precomputed",
            analysis_profile_id=CANONICAL_PROFILE_ID,
            evidence_contract_id=RESOLVER_COMPLETE_V2,
            **{f: getattr(p, f) for f in IDENTITY_FIELDS},
        ))
        # 'stale' row: identity/contract/shape all valid, but a leftover
        # best_eval_mate=3 the rerun now produces as None -> must be flagged.
        stale = mod.AnalysisResult(
            fen_before=START_FEN, move_uci="c2c3", move_san="c3",
            best_move_uci="e2e4", best_move_san="e4", best_line_uci="e2e4 e7e5",
            played_eval=0, played_eval_mate=None, best_eval=30, best_eval_mate=None,
            eval_delta=30, classification="good",
        )
        db.add(AnalysisCache(
            fen_before=stale.fen_before, move_uci=stale.move_uci, move_san="c3",
            best_move_uci="e2e4", best_move_san="e4", best_line_uci="e2e4 e7e5",
            classification="good", played_eval=0, best_eval=30, best_eval_mate=3,
            eval_delta=30, source="precomputed",
            analysis_profile_id=CANONICAL_PROFILE_ID,
            evidence_contract_id=RESOLVER_COMPLETE_V2,
            **{f: getattr(p, f) for f in IDENTITY_FIELDS},
        ))
        db.commit()
        failures = mod._verify_stored(
            db, [good, bad, stale], profile_id=CANONICAL_PROFILE_ID
        )

    assert (good.fen_before, good.move_uci) not in failures
    assert (bad.fen_before, bad.move_uci) in failures
    assert (stale.fen_before, stale.move_uci) in failures
    engine.dispose()


def test_worker_init_failure_records_error_and_aborts():
    """A worker that cannot launch/init its engine records an error and aborts,
    so survivors draining every key cannot yield a falsely-ok run."""
    import queue as _queue
    import threading as _threading

    mod = _load_script()
    work_queue = _queue.Queue()
    work_queue.put(mod.PositionToAnalyze(fen_before=START_FEN, move_uci="a2a3", move_san="a3"))
    work_queue.put(None)
    outcomes = {}
    worker_errors = []
    abort = _threading.Event()
    mod._worker_thread(
        0, work_queue, [], outcomes, _threading.Lock(), abort, worker_errors,
        [0], 1, 0.0, 24, "/nonexistent/stockfish-binary-xyz",
    )
    assert worker_errors, "init failure must be recorded"
    assert abort.is_set()
    # The position was never processed by this worker.
    assert (START_FEN, "a2a3") not in outcomes


def test_strict_manifest_write_failure_raises(tmp_path):
    """The terminal (strict) manifest write surfaces an OSError so a run cannot
    report success when the sidecar cannot be persisted; non-strict only logs."""
    mod = _load_script()
    bad = tmp_path / "no_such_dir" / "manifest.json"  # parent does not exist
    # Non-strict: best-effort, swallowed.
    mod._write_run_manifest(bad, "running", started_at=0.0)
    assert not bad.exists()
    # Strict: must raise.
    with pytest.raises(OSError):
        mod._write_run_manifest(bad, "ok", started_at=0.0, strict=True)
