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

/** Whether the current session's opening-score delta has finished its
 *  provably-fresh reconciliation. */
export type OpeningDeltaFreshness = "pending" | "fresh" | "unavailable";
export type OpeningDeltaSource = "opening_boundary" | "terminal";

export type SessionOpeningDelta = {
  sessionId: string;
  items: OpeningScoreDeltaItem[] | null;
  freshness: OpeningDeltaFreshness;
  source: OpeningDeltaSource;
  reconciliationToken: string;
};

/** Identity of the applied position a drill root confirmation is about. Every
 *  field is load-bearing: the confirmation's staleness guard re-checks all of
 *  them before acting on a late response. */
export type DrillRootConfirmRequest = {
  /** The decision the served move came from; null means the id was never returned. */
  decisionId: string | null;
  sessionId: string;
  /** The live FEN after applying the move. */
  fen: string;
  /** Plies played to reach `fen` — the boundary this confirmation claims. */
  ply: number;
  /** The move that reached the root. */
  uci: string;
};

/** An applied player move, as `applyPlayerMove` reports it. */
export type AppliedPlayerMove = {
  fenAfter: string;
  fenBefore: string;
  uciHistory: string[];
  gameOver: boolean;
  moveIndex: number;
  moveSan: string;
  moveUci: string;
};

let openingDeltaReconciliationNonce = 0;

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
  /** Last server-acknowledged branch token for the active session move line. */
  moveLineRevision: number;
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
  /** A root-reaching move applied to the board that the backend has not yet
   *  confirmed. NON-NULL ⇒ the drill is NOT root_reached and no further gameplay
   *  may proceed; it covers both the in-flight and the failed case (§17.4.1).
   *
   *  It lives in the store rather than in ChessGame because it must survive a
   *  remount exactly as far as the board and the session do: ChessGame
   *  reconstructs Chess from `liveFen` on mount, so a component-local barrier
   *  would vanish on a route round trip and leave the applied root position
   *  playable — the next player move would then run past an unconfirmed root and
   *  fail the drill off-route. Not persisted: a reload drops the board with it.
   *
   *  The stored object's REFERENCE is also the owning attempt's token — only the
   *  attempt whose object is still here may clear the barrier. */
  drillRootConfirm: DrillRootConfirmRequest | null;
  /** The applied player move whose pre-root route-check has not settled. That
   *  check IS the boundary confirmation when the PLAYER is the one who moves into
   *  the root, so the pending work is durable for the same reason
   *  `drillRootConfirm` is: it must survive a remount (the drill is otherwise
   *  stranded — opponent to move, nothing left to re-drive it) and it must be
   *  invalidated the moment its move leaves live history, or a Retry would submit
   *  a proof for a move no longer on the board. Single source of truth: the
   *  `player-route` recovery carries no payload of its own. */
  drillPendingRouteMove: AppliedPlayerMove | null;
  playerRating: number;
  isProvisional: boolean;
  ratingScores: RatingScores;
  ratingChange: RatingChange | null;
  scoreChanges: RatingScores | null;
  /** Opening-score deltas owned by the current session, during live boundary
   *  or terminal reconciliation. Results from replaced sessions are ignored. */
  openingScoreDelta: SessionOpeningDelta | null;
  /** Monotonic token invalidating in-flight delta polls. Deliberate abandonment
   *  (handleReset) bumps it; a poll carrying a stale token is dropped at COMMIT
   *  time, closing the race where a response resolves between abort and commit. */
  openingDeltaPollToken: number;
  soundMuted: boolean;
  soundVolume: number;
};

export type GameActions = {
  setLiveFen: (update: SetStateAction<string>) => void;
  setMoveHistory: (update: SetStateAction<MoveRecord[]>) => void;
  setViewIndex: (update: SetStateAction<number | null>) => void;
  setSessionId: (update: SetStateAction<string | null>) => void;
  setMoveLineRevision: (update: SetStateAction<number>) => void;
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
  setDrillRootConfirm: (
    update: SetStateAction<DrillRootConfirmRequest | null>,
  ) => void;
  setDrillPendingRouteMove: (
    update: SetStateAction<AppliedPlayerMove | null>,
  ) => void;
  setPlayerRating: (update: SetStateAction<number>) => void;
  setIsProvisional: (update: SetStateAction<boolean>) => void;
  setRatingScores: (update: SetStateAction<RatingScores>) => void;
  setRatingChange: (update: SetStateAction<RatingChange | null>) => void;
  setScoreChanges: (update: SetStateAction<RatingScores | null>) => void;
  /** Record the terminal endpoint's warm delta for `sessionId`. */
  setTerminalOpeningDelta: (
    sessionId: string,
    items: OpeningScoreDeltaItem[] | null,
  ) => void;
  /** Claim provisional live ownership only for the current active session. */
  setBoundaryOpeningDeltaPending: (
    sessionId: string,
    reconciliationToken: string,
  ) => void;
  /** Commit a reconciled poll result. Compares against the LIVE sessionId and
   *  poll token inside the updater, so the whole decision is one atomic
   *  transition against the state the commit actually lands on. */
  applyPolledOpeningDelta: (
    sessionId: string,
    items: OpeningScoreDeltaItem[] | null,
    pollToken: number,
    source?: OpeningDeltaSource,
    reconciliationToken?: string,
  ) => void;
  /** Release a matching pending current-session gate when polling genuinely
   *  gives up. Retains the warm items for display. */
  markOpeningDeltaUnavailable: (
    sessionId: string,
    pollToken: number,
    source?: OpeningDeltaSource,
    reconciliationToken?: string,
  ) => void;
  clearBoundaryOpeningDelta: (
    sessionId: string,
    reconciliationToken?: string,
  ) => void;
  /** Flip to a new session and clear the current delta slot as ONE transaction. */
  beginSession: (sessionId: string, moveLineRevision?: number) => void;
  /** Clear the current session's delta slot. */
  clearOpeningDelta: () => void;
  /** Deliberate abandonment: clear the slot and invalidate in-flight polls. */
  abandonOpeningDeltas: () => void;
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
  moveLineRevision: 0,
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
  drillRootConfirm: null,
  drillPendingRouteMove: null,
  playerRating: 1200,
  isProvisional: true,
  ratingScores: {
    elo: { rating: 1200, is_provisional: true },
    chesscom: null,
    lichess: null,
  },
  ratingChange: null,
  scoreChanges: null,
  openingScoreDelta: null,
  openingDeltaPollToken: 0,
  soundMuted: getSoundMuted(),
  soundVolume: getSoundVolume(),

  // --- Actions ---
  setLiveFen: (u) => set((s) => ({ liveFen: resolve(u, s.liveFen) })),
  setMoveHistory: (u) =>
    set((s) => ({ moveHistory: resolve(u, s.moveHistory) })),
  setViewIndex: (u) => set((s) => ({ viewIndex: resolve(u, s.viewIndex) })),
  setSessionId: (u) => set((s) => ({ sessionId: resolve(u, s.sessionId) })),
  setMoveLineRevision: (u) =>
    set((s) => ({ moveLineRevision: resolve(u, s.moveLineRevision) })),
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
  setDrillRootConfirm: (u) =>
    set((s) => ({ drillRootConfirm: resolve(u, s.drillRootConfirm) })),
  setDrillPendingRouteMove: (u) =>
    set((s) => ({ drillPendingRouteMove: resolve(u, s.drillPendingRouteMove) })),
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
  setTerminalOpeningDelta: (sessionId, items) =>
    set(() => {
      openingDeltaReconciliationNonce += 1;
      return {
        openingScoreDelta: {
          sessionId,
          items,
          freshness: "pending" as const,
          source: "terminal" as const,
          reconciliationToken: `terminal:${openingDeltaReconciliationNonce}`,
        },
      };
    }),

  setBoundaryOpeningDeltaPending: (sessionId, reconciliationToken) =>
    set((s) => {
      if (s.sessionId !== sessionId || !s.isGameActive) return {};
      if (s.openingScoreDelta?.source === "terminal") return {};
      if (
        s.openingScoreDelta?.source === "opening_boundary" &&
        s.openingScoreDelta.reconciliationToken === reconciliationToken
      ) {
        return {};
      }
      return {
        openingScoreDelta: {
          sessionId,
          items: null,
          freshness: "pending" as const,
          source: "opening_boundary" as const,
          reconciliationToken,
        },
      };
    }),

  applyPolledOpeningDelta: (
    sessionId,
    items,
    pollToken,
    requestedSource,
    requestedToken,
  ) =>
    set((s) => {
      // Only the live session can reconcile, including while its replacement
      // request is pending. Reset invalidates all earlier poll tokens.
      if (pollToken !== s.openingDeltaPollToken || sessionId !== s.sessionId) {
        return {};
      }
      const source = requestedSource ?? s.openingScoreDelta?.source ?? "terminal";
      const reconciliationToken =
        requestedToken ??
        s.openingScoreDelta?.reconciliationToken ??
        `terminal:legacy:${pollToken}`;
      const delta: SessionOpeningDelta & { freshness: "fresh" } = {
        sessionId,
        items,
        freshness: "fresh",
        source,
        reconciliationToken,
      };
      const owner = s.openingScoreDelta;
      if (
        owner &&
        (owner.source !== source ||
          owner.reconciliationToken !== reconciliationToken)
      ) {
        return {};
      }
      return { openingScoreDelta: delta };
    }),

  markOpeningDeltaUnavailable: (
    sessionId,
    pollToken,
    requestedSource,
    requestedToken,
  ) =>
    set((s) => {
      if (pollToken !== s.openingDeltaPollToken) return {};
      if (s.sessionId !== sessionId) return {};
      if (
        s.openingScoreDelta?.sessionId !== sessionId ||
        s.openingScoreDelta.freshness !== "pending" ||
        (requestedSource !== undefined &&
          s.openingScoreDelta.source !== requestedSource) ||
        (requestedToken !== undefined &&
          s.openingScoreDelta.reconciliationToken !== requestedToken)
      ) {
        return {};
      }
      return {
        openingScoreDelta: {
          ...s.openingScoreDelta,
          freshness: "unavailable" as const,
        },
      };
    }),

  clearBoundaryOpeningDelta: (sessionId, reconciliationToken) =>
    set((s) => {
      const current = s.openingScoreDelta;
      if (
        current?.sessionId !== sessionId ||
        current.source !== "opening_boundary" ||
        (reconciliationToken !== undefined &&
          current.reconciliationToken !== reconciliationToken)
      ) {
        return {};
      }
      return { openingScoreDelta: null };
    }),

  // Replace session ownership and clear its predecessor's delta atomically.
  // Old polls may finish for telemetry, but cannot commit to the new session.
  beginSession: (sessionId, moveLineRevision = 0) =>
    set(() => ({
      sessionId,
      moveLineRevision,
      openingScoreDelta: null,
    })),

  clearOpeningDelta: () => set(() => ({ openingScoreDelta: null })),

  abandonOpeningDeltas: () =>
    set((s) => ({
      openingScoreDelta: null,
      openingDeltaPollToken: s.openingDeltaPollToken + 1,
    })),

  setSoundMuted: (u) =>
    set((s) => ({ soundMuted: persistSoundMuted(resolve(u, s.soundMuted)) })),
  setSoundVolume: (u) =>
    set((s) => ({ soundVolume: persistSoundVolume(resolve(u, s.soundVolume)) })),
}));
