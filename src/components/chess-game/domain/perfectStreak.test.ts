import { describe, expect, it } from "vitest";
import type { AnalysisResult } from "../../../hooks/useMoveAnalysis";
import type { MoveRecord } from "./movePresentation";
import { derivePerfectStreak } from "./perfectStreak";

const moves: MoveRecord[] = [
  { san: "e4", fen: "fen-1", uci: "e2e4" },
  { san: "e5", fen: "fen-2", uci: "e7e5" },
  { san: "Nf3", fen: "fen-3", uci: "g1f3" },
  { san: "Nc6", fen: "fen-4", uci: "b8c6" },
  { san: "Bb5", fen: "fen-5", uci: "f1b5" },
  { san: "a6", fen: "fen-6", uci: "a7a6" },
  { san: "Ba4", fen: "fen-7", uci: "b5a4" },
];

const analysis = (
  moveIndex: number,
  classification: AnalysisResult["classification"],
  move = moves[moveIndex].uci,
): AnalysisResult => ({
  id: `a-${moveIndex}`,
  move,
  bestMove: move,
  bestEval: 0,
  playedEval: 0,
  currentPositionEval: 0,
  moveIndex,
  delta: 0,
  classification,
  blunder: false,
  recordable: false,
});

describe("derivePerfectStreak", () => {
  it("recomputes player-only streaks by move order and ignores opponent best moves", () => {
    const result = derivePerfectStreak({
      moveHistory: moves,
      analysisMap: new Map([
        [0, analysis(0, "best")],
        [1, analysis(1, "best")],
        [2, analysis(2, "best")],
        [4, analysis(4, "good")],
        [6, analysis(6, "best")],
      ]),
      playerColor: "white",
      previousPersonalBest: 0,
      recordPersonalBest: 0,
      previousState: null,
      celebratedEventKeys: new Set(),
    });

    expect(result.current).toBe(1);
    expect(result.bestInHistory).toBe(2);
    expect(result.personalBest).toBe(2);
    expect(result.event).toBeNull();
  });

  it("ignores stale UCI mismatches and null classifications", () => {
    const result = derivePerfectStreak({
      moveHistory: moves.slice(0, 5),
      analysisMap: new Map([
        [0, analysis(0, "best")],
        [2, analysis(2, "best", "d2d4")],
        [4, analysis(4, null)],
        [8, analysis(0, "best")],
      ]),
      playerColor: "white",
      previousPersonalBest: 0,
      recordPersonalBest: 0,
      previousState: null,
      celebratedEventKeys: new Set(),
    });

    expect(result.current).toBe(1);
    expect(result.bestInHistory).toBe(1);
  });

  it("emits milestone and record events only on threshold crossing", () => {
    const analysisMap = new Map([
      [0, analysis(0, "best")],
      [2, analysis(2, "best")],
      [4, analysis(4, "best")],
      [6, analysis(6, "best")],
    ]);

    const milestone = derivePerfectStreak({
      moveHistory: moves.slice(0, 5),
      analysisMap,
      playerColor: "white",
      previousPersonalBest: 10,
      recordPersonalBest: 10,
      previousState: {
        current: 2,
        bestInHistory: 2,
        personalBest: 10,
        recordPersonalBest: 10,
      },
      celebratedEventKeys: new Set(),
    });
    expect(milestone.event).toEqual({
      type: "milestone",
      streak: 3,
      key: "milestone:3",
    });

    const replay = derivePerfectStreak({
      moveHistory: moves,
      analysisMap,
      playerColor: "white",
      previousPersonalBest: 10,
      recordPersonalBest: 10,
      previousState: {
        current: 4,
        bestInHistory: 4,
        personalBest: 10,
        recordPersonalBest: 10,
      },
      celebratedEventKeys: new Set(["milestone:3"]),
    });
    expect(replay.event).toBeNull();

    const record = derivePerfectStreak({
      moveHistory: moves,
      analysisMap,
      playerColor: "white",
      previousPersonalBest: 3,
      recordPersonalBest: 3,
      previousState: {
        current: 3,
        bestInHistory: 3,
        personalBest: 3,
        recordPersonalBest: 3,
      },
      celebratedEventKeys: new Set(),
    });
    expect(record.event).toEqual({
      type: "record",
      streak: 4,
      key: "record:4",
    });
  });

  it("emits record events when best history increases even if current streak is broken", () => {
    const result = derivePerfectStreak({
      moveHistory: moves,
      analysisMap: new Map([
        [0, analysis(0, "best")],
        [2, analysis(2, "best")],
        [4, analysis(4, "best")],
        [6, analysis(6, "good")],
      ]),
      playerColor: "white",
      previousPersonalBest: 2,
      recordPersonalBest: 2,
      previousState: {
        current: 0,
        bestInHistory: 2,
        personalBest: 2,
        recordPersonalBest: 2,
      },
      celebratedEventKeys: new Set(),
    });

    expect(result.current).toBe(0);
    expect(result.bestInHistory).toBe(3);
    expect(result.event).toEqual({
      type: "record",
      streak: 3,
      key: "record:3",
    });
  });

  it("does not emit record events before the all-time baseline is loaded", () => {
    const result = derivePerfectStreak({
      moveHistory: moves,
      analysisMap: new Map([
        [0, analysis(0, "best")],
        [2, analysis(2, "best")],
      ]),
      playerColor: "white",
      previousPersonalBest: 0,
      recordPersonalBest: null,
      previousState: {
        current: 1,
        bestInHistory: 1,
        personalBest: 1,
        recordPersonalBest: null,
      },
      celebratedEventKeys: new Set(),
    });

    expect(result.personalBest).toBe(2);
    expect(result.event).toBeNull();
  });

  it("emits a delayed record when the baseline loads below an already-observed best", () => {
    const analysisMap = new Map([
      [0, analysis(0, "best")],
      [2, analysis(2, "best")],
      [4, analysis(4, "best")],
      [6, analysis(6, "good")],
    ]);
    const beforeBaseline = derivePerfectStreak({
      moveHistory: moves,
      analysisMap,
      playerColor: "white",
      previousPersonalBest: 0,
      recordPersonalBest: null,
      previousState: { current: 2, bestInHistory: 2, personalBest: 2, recordPersonalBest: null },
      celebratedEventKeys: new Set(),
    });

    expect(beforeBaseline.event).toBeNull();

    const afterBaseline = derivePerfectStreak({
      moveHistory: moves,
      analysisMap,
      playerColor: "white",
      previousPersonalBest: beforeBaseline.personalBest,
      recordPersonalBest: 2,
      previousState: {
        current: beforeBaseline.current,
        bestInHistory: beforeBaseline.bestInHistory,
        personalBest: beforeBaseline.personalBest,
        recordPersonalBest: null,
      },
      celebratedEventKeys: new Set(),
    });

    expect(afterBaseline.current).toBe(0);
    expect(afterBaseline.bestInHistory).toBe(3);
    expect(afterBaseline.event).toEqual({
      type: "record",
      streak: 3,
      key: "record:3",
    });
  });

  it("does not re-emit the same locally achieved record after game reset", () => {
    const recordThreeMap = new Map([
      [0, analysis(0, "best")],
      [2, analysis(2, "best")],
      [4, analysis(4, "best")],
    ]);
    const firstGame = derivePerfectStreak({
      moveHistory: moves.slice(0, 5),
      analysisMap: recordThreeMap,
      playerColor: "white",
      previousPersonalBest: 0,
      recordPersonalBest: 0,
      previousState: {
        current: 2,
        bestInHistory: 2,
        personalBest: 2,
        recordPersonalBest: 0,
      },
      celebratedEventKeys: new Set(),
    });

    expect(firstGame.event).toEqual({
      type: "record",
      streak: 3,
      key: "record:3",
    });

    const secondGame = derivePerfectStreak({
      moveHistory: moves.slice(0, 5),
      analysisMap: recordThreeMap,
      playerColor: "white",
      previousPersonalBest: firstGame.personalBest,
      recordPersonalBest: 0,
      previousState: {
        current: 2,
        bestInHistory: 2,
        personalBest: firstGame.personalBest,
        recordPersonalBest: 0,
      },
      celebratedEventKeys: new Set(),
    });

    expect(secondGame.personalBest).toBe(3);
    expect(secondGame.event?.type).not.toBe("record");
  });
});
