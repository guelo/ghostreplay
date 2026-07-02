import type { SetStateAction } from "react";
import { create } from "zustand";
import type { DrillSessionState, DrillStrictness, OpeningScoreDeltaItem, RatingChange, RatingScoreKey, RatingScores } from "../utils/api";
import type { MoveRecord } from "../components/chess-game/domain/movePresentation";
import type { GameResult } from "../components/chess-game/domain/status";
import {
  getSoundMuted,
  getSoundVolume,
  setSoundMuted as persistSoundMuted,
  setSoundVolume as persistSoundVolume,
} from "../utils/soundSettings";

type BoardOrientation = "white" | "black";

/** Resolve a React-style SetStateAction (value or updater function). */
const resolve = <T>(update: SetStateAction<T>, prev: T): T =>
  typeof update === "function"
    ? (update as (prev: T) => T)(prev)
    : update;

export const getRatingDisplayLabel = (key: RatingScoreKey): string => {
  if (key === "chesscom") return "Chess.com";
  if (key === "lichess") return "Lichess";
  return "Elo";
};

export type GameState = {
  // --- Game position (hot, changes every move) ---
  /** Authoritative live board position from the game engine. */
  liveFen: string;
  /** Canonical move list for the current game. */
  moveHistory: MoveRecord[];
  /** Selected history index, or null when viewing live position. */
  viewIndex: number | null;

  // --- Session (set once per game, rarely changes) ---
  sessionId: string | null;
  isGameActive: boolean;
  gameResult: GameResult | null;
  playerColor: BoardOrientation;
  playerColorChoice: BoardOrientation | "random";
  boardOrientation: BoardOrientation;
  engineElo: number;
  isRated: boolean;
  isPracticeContinuation: boolean;
  drillOpeningKey: string | null;
  // Ad-hoc card drills: the full UCI line to the target FEN. Durable (not a
  // component ref) so it survives the /drill-analysis route round trip and the
  // reviewed-return "Again" can replay a non-root drill. null for registered
  // roots (routed via the book BFS, no line needed).
  drillLine: string[] | null;
  drillOpeningName: string | null;
  drillState: DrillSessionState | null;
  drillStrictness: DrillStrictness | null;
  drillStrictnessCp: number | null;
  drillTerminalReason: 'off_route' | 'accuracy' | 'natural_end' | null;
  playerRating: number;
  isProvisional: boolean;
  ratingScores: RatingScores;
  ratingChange: RatingChange | null;
  scoreChanges: RatingScores | null;
  // Per-played-opening score deltas for the just-ended game/drill (g-xanz).
  openingScoreChanges: OpeningScoreDeltaItem[] | null;
  soundMuted: boolean;
  soundVolume: number;
};

export type GameActions = {
  setLiveFen: (update: SetStateAction<string>) => void;
  setMoveHistory: (update: SetStateAction<MoveRecord[]>) => void;
  setViewIndex: (update: SetStateAction<number | null>) => void;
  setSessionId: (update: SetStateAction<string | null>) => void;
  setIsGameActive: (update: SetStateAction<boolean>) => void;
  setGameResult: (update: SetStateAction<GameResult | null>) => void;
  setPlayerColor: (update: SetStateAction<BoardOrientation>) => void;
  setPlayerColorChoice: (
    update: SetStateAction<BoardOrientation | "random">,
  ) => void;
  setBoardOrientation: (update: SetStateAction<BoardOrientation>) => void;
  setEngineElo: (update: SetStateAction<number>) => void;
  setIsRated: (update: SetStateAction<boolean>) => void;
  setIsPracticeContinuation: (update: SetStateAction<boolean>) => void;
  setDrillOpeningKey: (update: SetStateAction<string | null>) => void;
  setDrillLine: (update: SetStateAction<string[] | null>) => void;
  setDrillOpeningName: (update: SetStateAction<string | null>) => void;
  setDrillState: (update: SetStateAction<DrillSessionState | null>) => void;
  setDrillStrictness: (
    update: SetStateAction<DrillStrictness | null>,
  ) => void;
  setDrillStrictnessCp: (update: SetStateAction<number | null>) => void;
  setDrillTerminalReason: (
    update: SetStateAction<'off_route' | 'accuracy' | 'natural_end' | null>,
  ) => void;
  setPlayerRating: (update: SetStateAction<number>) => void;
  setIsProvisional: (update: SetStateAction<boolean>) => void;
  setRatingScores: (update: SetStateAction<RatingScores>) => void;
  setRatingChange: (update: SetStateAction<RatingChange | null>) => void;
  setScoreChanges: (update: SetStateAction<RatingScores | null>) => void;
  setOpeningScoreChanges: (
    update: SetStateAction<OpeningScoreDeltaItem[] | null>,
  ) => void;
  setSoundMuted: (update: SetStateAction<boolean>) => void;
  setSoundVolume: (update: SetStateAction<number>) => void;
};

const STARTING_FEN =
  "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

export const useGameStore = create<GameState & GameActions>((set) => ({
  // --- State ---
  liveFen: STARTING_FEN,
  moveHistory: [],
  viewIndex: null,
  sessionId: null,
  isGameActive: false,
  gameResult: null,
  playerColor: "white",
  playerColorChoice: "random",
  boardOrientation: "white",
  engineElo: 800,
  isRated: true,
  isPracticeContinuation: false,
  drillOpeningKey: null,
  drillLine: null,
  drillOpeningName: null,
  drillState: null,
  drillStrictness: null,
  drillStrictnessCp: null,
  drillTerminalReason: null,
  playerRating: 1200,
  isProvisional: true,
  ratingScores: {
    elo: { rating: 1200, is_provisional: true },
    chesscom: null,
    lichess: null,
  },
  ratingChange: null,
  scoreChanges: null,
  openingScoreChanges: null,
  soundMuted: getSoundMuted(),
  soundVolume: getSoundVolume(),

  // --- Actions ---
  setLiveFen: (u) => set((s) => ({ liveFen: resolve(u, s.liveFen) })),
  setMoveHistory: (u) =>
    set((s) => ({ moveHistory: resolve(u, s.moveHistory) })),
  setViewIndex: (u) => set((s) => ({ viewIndex: resolve(u, s.viewIndex) })),
  setSessionId: (u) => set((s) => ({ sessionId: resolve(u, s.sessionId) })),
  setIsGameActive: (u) =>
    set((s) => ({ isGameActive: resolve(u, s.isGameActive) })),
  setGameResult: (u) =>
    set((s) => ({ gameResult: resolve(u, s.gameResult) })),
  setPlayerColor: (u) =>
    set((s) => ({ playerColor: resolve(u, s.playerColor) })),
  setPlayerColorChoice: (u) =>
    set((s) => ({ playerColorChoice: resolve(u, s.playerColorChoice) })),
  setBoardOrientation: (u) =>
    set((s) => ({ boardOrientation: resolve(u, s.boardOrientation) })),
  setEngineElo: (u) => set((s) => ({ engineElo: resolve(u, s.engineElo) })),
  setIsRated: (u) => set((s) => ({ isRated: resolve(u, s.isRated) })),
  setIsPracticeContinuation: (u) =>
    set((s) => ({
      isPracticeContinuation: resolve(u, s.isPracticeContinuation),
    })),
  setDrillOpeningKey: (u) =>
    set((s) => ({ drillOpeningKey: resolve(u, s.drillOpeningKey) })),
  setDrillLine: (u) => set((s) => ({ drillLine: resolve(u, s.drillLine) })),
  setDrillOpeningName: (u) =>
    set((s) => ({ drillOpeningName: resolve(u, s.drillOpeningName) })),
  setDrillState: (u) =>
    set((s) => ({ drillState: resolve(u, s.drillState) })),
  setDrillStrictness: (u) =>
    set((s) => ({ drillStrictness: resolve(u, s.drillStrictness) })),
  setDrillStrictnessCp: (u) =>
    set((s) => ({ drillStrictnessCp: resolve(u, s.drillStrictnessCp) })),
  setDrillTerminalReason: (u) =>
    set((s) => ({ drillTerminalReason: resolve(u, s.drillTerminalReason) })),
  setPlayerRating: (u) =>
    set((s) => ({ playerRating: resolve(u, s.playerRating) })),
  setIsProvisional: (u) =>
    set((s) => ({ isProvisional: resolve(u, s.isProvisional) })),
  setRatingScores: (u) =>
    set((s) => ({ ratingScores: resolve(u, s.ratingScores) })),
  setRatingChange: (u) =>
    set((s) => ({ ratingChange: resolve(u, s.ratingChange) })),
  setScoreChanges: (u) =>
    set((s) => ({ scoreChanges: resolve(u, s.scoreChanges) })),
  setOpeningScoreChanges: (u) =>
    set((s) => ({ openingScoreChanges: resolve(u, s.openingScoreChanges) })),
  setSoundMuted: (u) =>
    set((s) => ({ soundMuted: persistSoundMuted(resolve(u, s.soundMuted)) })),
  setSoundVolume: (u) =>
    set((s) => ({ soundVolume: persistSoundVolume(resolve(u, s.soundVolume)) })),
}));
