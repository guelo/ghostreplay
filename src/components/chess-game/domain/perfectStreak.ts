import type { AnalysisResult } from "../../../hooks/useMoveAnalysis";
import type { MoveRecord } from "./movePresentation";

export type PerfectStreakEvent = {
  type: "milestone" | "record";
  streak: number;
  key: string;
};

export type PerfectStreakState = {
  current: number;
  bestInHistory: number;
  personalBest: number;
  event: PerfectStreakEvent | null;
};

export type PreviousPerfectStreakState = {
  current: number;
  bestInHistory: number;
  personalBest: number;
  recordPersonalBest: number | null;
};

const milestoneFor = (streak: number): number | null => {
  if (streak === 3) return 3;
  if (streak >= 5 && streak % 5 === 0) return streak;
  return null;
};

const isPlayerMoveIndex = (
  index: number,
  playerColor: "white" | "black",
): boolean => {
  const isWhiteMove = index % 2 === 0;
  return playerColor === "white" ? isWhiteMove : !isWhiteMove;
};

export const derivePerfectStreak = ({
  moveHistory,
  analysisMap,
  playerColor,
  previousPersonalBest,
  recordPersonalBest,
  previousState,
  celebratedEventKeys,
}: {
  moveHistory: MoveRecord[];
  analysisMap: Map<number, AnalysisResult>;
  playerColor: "white" | "black";
  previousPersonalBest: number;
  recordPersonalBest: number | null;
  previousState: PreviousPerfectStreakState | null;
  celebratedEventKeys: ReadonlySet<string>;
}): PerfectStreakState => {
  let current = 0;
  let bestInHistory = 0;

  for (let index = 0; index < moveHistory.length; index += 1) {
    if (!isPlayerMoveIndex(index, playerColor)) {
      continue;
    }

    const move = moveHistory[index];
    const analysis = analysisMap.get(index);
    if (!analysis || analysis.move !== move.uci || analysis.classification == null) {
      continue;
    }

    if (analysis.classification === "best") {
      current += 1;
      bestInHistory = Math.max(bestInHistory, current);
    } else {
      current = 0;
    }
  }

  const personalBest = Math.max(previousPersonalBest, bestInHistory);
  let event: PerfectStreakEvent | null = null;

  if (previousState) {
    const baselineJustLoaded = previousState.recordPersonalBest === null;
    const effectiveRecordBest = baselineJustLoaded
      ? recordPersonalBest
      : Math.max(recordPersonalBest ?? 0, previousPersonalBest);
    const recordKey = `record:${bestInHistory}`;
    if (
      recordPersonalBest !== null &&
      bestInHistory > 0 &&
      (bestInHistory > previousState.bestInHistory ||
        baselineJustLoaded) &&
      effectiveRecordBest !== null &&
      bestInHistory > effectiveRecordBest &&
      !celebratedEventKeys.has(recordKey)
    ) {
      event = { type: "record", streak: bestInHistory, key: recordKey };
    } else {
      const milestone = milestoneFor(current);
      const milestoneKey = milestone ? `milestone:${milestone}` : null;
      if (
        milestone &&
        previousState.current < milestone &&
        current >= milestone &&
        !celebratedEventKeys.has(milestoneKey!)
      ) {
        event = { type: "milestone", streak: milestone, key: milestoneKey! };
      }
    }
  }

  return {
    current,
    bestInHistory,
    personalBest,
    event,
  };
};
