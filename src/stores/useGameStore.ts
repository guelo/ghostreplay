import type { SetStateAction } from "react";
import { create } from "zustand";
import type { RatingChange } from "../utils/api";
import type { MoveRecord } from "../components/chess-game/domain/movePresentation";
import type { GameResult } from "../components/chess-game/domain/status";

type BoardOrientation = "white" | "black";

/** Resolve a React-style SetStateAction (value or updater function). */
const resolve = <T>(update: SetStateAction<T>, prev: T): T =>
  typeof update === "function"
    ? (update as (prev: T) => T)(prev)
    : update;

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
  playerRating: number;
  isProvisional: boolean;
  ratingChange: RatingChange | null;
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
  setPlayerRating: (update: SetStateAction<number>) => void;
  setIsProvisional: (update: SetStateAction<boolean>) => void;
  setRatingChange: (update: SetStateAction<RatingChange | null>) => void;
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
  playerRating: 1200,
  isProvisional: true,
  ratingChange: null,

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
  setPlayerRating: (u) =>
    set((s) => ({ playerRating: resolve(u, s.playerRating) })),
  setIsProvisional: (u) =>
    set((s) => ({ isProvisional: resolve(u, s.isProvisional) })),
  setRatingChange: (u) =>
    set((s) => ({ ratingChange: resolve(u, s.ratingChange) })),
}));
