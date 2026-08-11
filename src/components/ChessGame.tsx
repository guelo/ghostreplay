import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent, SetStateAction } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Chess } from "chess.js";
import type { Square } from "chess.js";
import type { PieceDropHandlerArgs } from "react-chessboard";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { useChessGameLifecycle } from "../hooks/useChessGameLifecycle";
import { useChessGameController } from "../hooks/useChessGameController";
import type { PlayerMoveApplyResult } from "../hooks/useChessGameController";
import { useOpponentMove } from "../hooks/useOpponentMove";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { useBoardNotice } from "../hooks/useBoardNotice";
import { useSessionOpenings } from "../hooks/useSessionOpenings";
import { useSessionUploadCommitRevision } from "../hooks/useSessionUploadCommitRevision";
import { useLiveOpeningLineage } from "../hooks/useLiveOpeningLineage";
import { GAME_MOBILE_QUERY } from "../styles/breakpoints";
import { useGameStore } from "../stores/useGameStore";
import type { DrillRootConfirmRequest } from "../stores/useGameStore";
import {
  getOpeningDeltaPollSnapshot,
  getOpeningDeltaVisibility,
  pollFreshOpeningDelta,
} from "../utils/openingDeltaPoll";
import { useLastDrillDeltaToast } from "../hooks/useLastDrillDeltaToast";
import { strictnessFromCp } from "./chess-game/ui/DrillSetupPanel.helpers";
import type { OpeningLineageItem, OpeningRootItem } from "../utils/api";
import { checkDrillRoute, failDrill, getOpeningRoots } from "../utils/api";
import {
  gameAnalysisStore,
  AnalysisStoreProvider,
} from "../stores/createAnalysisStore";
import { useGameAnalysisCoordinator } from "../contexts/useGameAnalysisCoordinator";
import type { DrillGrade } from "../services/GameAnalysisCoordinator";
import { captureEvent } from "../analytics/posthog";
import type { TargetBlunderSrs } from "../utils/api";
import { getStatsAchievements } from "../utils/api";
import {
  buildBlunderAlert,
  deriveBlunderArrows,
  deriveLastMoveSquares,
  type BlunderAlert,
  type DrillFailInfo,
  type ReviewFailInfo,
} from "./chess-game/domain/movePresentation";
import { classifyDrillAgainInput } from "./chess-game/domain/drillAgainActivation";
import {
  derivePerfectStreak,
  type PreviousPerfectStreakState,
  type PerfectStreakEvent,
} from "./chess-game/domain/perfectStreak";
import { hasReviewTargetAtFen } from "./chess-game/domain/reviewState";
import {
  deriveGameStatusBadge,
  deriveStatusText,
} from "./chess-game/domain/status";
import type { GameResult } from "./chess-game/domain/status";
import type { EndGameFanfareTrigger } from "./chess-game/ui/EndGameFanfare";
import {
  MAIA_BOT_NAMES,
  MAIA_ELO_BINS,
  ROUTE_CHECK_TIMEOUT_MS,
  STARTING_FEN,
} from "./chess-game/config";
import { sampleDrillEloBin } from "./chess-game/elo";
import type {
  CopyPositionNotice,
  OpenHistoryOptions,
  ResolvedReview,
} from "./chess-game/types";
import BoardStage from "./chess-game/ui/BoardStage";
import type { StartDrillDraft } from "./chess-game/ui/StartPanel";
import GameInfoPanel from "./chess-game/ui/GameInfoPanel";
import GameOpeningLineage from "./GameOpeningLineage";
import PostGameBanner from "./chess-game/ui/PostGameBanner";
import DrillStopActions from "./chess-game/ui/DrillStopActions";
import {
  buildDrillAnalysisSnapshot,
  type DrillAnalysisSnapshot,
} from "./chess-game/domain/sessionUpload";
import { useDrillAnalysisStore } from "../stores/drillAnalysisStore";
import MaterialDisplay from "./MaterialDisplay";
import type { MoveMessage, SrsFailDetail } from "./MoveList";
import {
  ConnectedEvalBar,
  ConnectedAnalysisGraph,
  ConnectedMoveList,
} from "./chess-game/AnalysisConnectors";
import AnalysisEffects from "./chess-game/AnalysisEffects";

type ChessGameProps = {
  onOpenHistory?: (options: OpenHistoryOptions) => void;
};

const isSquare = (value: string): value is Square => /^[a-h][1-8]$/.test(value);

const STRICTNESS_TIER_THRESHOLDS = {
  strict: 15,
  standard: 35,
  lenient: 50,
} as const;

const resolveStrictnessCp = (
  strictnessCp: number | null,
  strictness: "strict" | "standard" | "lenient" | null,
) => strictnessCp ?? STRICTNESS_TIER_THRESHOLDS[strictness ?? "standard"];

type DrillRecovery =
  | { kind: "analysis"; result: Extract<PlayerMoveApplyResult, { applied: true }> }
  | { kind: "opponent"; fen: string; uciHistory: string[] }
  // A root confirmation that failed. Retry re-issues it; the applied board stays.
  | { kind: "root-confirm"; request: DrillRootConfirmRequest }
  // A player-arrival route-check that failed AT the root. Retry must re-issue the
  // route-check, not the opponent request — the latter would advance the drill
  // with the evidence boundary never stamped. Carries no payload: the move is held
  // durably in `drillPendingRouteMove`, which is also what invalidates it.
  | { kind: "player-route" };

/** Router marker placed by DrillAnalysisPage's "Back to drill" control (g-65ve). */
type ReturnFromDrillAnalysisMarker = {
  returnFromDrillAnalysis?: { sourceSessionId?: string | null };
};

/**
 * Decide synchronously whether the current mount is a valid "return to the
 * just-reviewed drill" (g-65ve). Returns true only when the router marker, the
 * transient analysis snapshot, and the retained game store all describe the
 * same abandoned drill AND every restart setting needed to replay it is present.
 *
 * Identity is bound through the session ID end-to-end — never inferred from
 * opening key, moves, or reusable settings — so a stale snapshot can never be
 * paired with a different game. Any failure falls back to ordinary /play.
 */
const isReviewedDrillReturnValid = (
  locationState: unknown,
  snapshot: DrillAnalysisSnapshot | null,
  store: ReturnType<typeof useGameStore.getState>,
): boolean => {
  const marker = (locationState as ReturnFromDrillAnalysisMarker | null)
    ?.returnFromDrillAnalysis;
  if (!marker?.sourceSessionId) return false;
  if (!snapshot) return false;
  if (marker.sourceSessionId !== snapshot.sourceSessionId) return false;
  if (snapshot.sourceSessionId !== store.sessionId) return false;
  if (store.isGameActive !== false) return false;
  if (store.drillState !== "abandoned") return false;
  if (!store.drillOpeningKey) return false;
  if (store.moveHistory.length === 0) return false;
  // Restart replays opening/side/strictness exactly (difficulty is resampled),
  // so require those handleAgainDrill inputs — but not engineElo, which is no
  // longer a restart input.
  if (store.playerColor !== "white" && store.playerColor !== "black") return false;
  if (store.drillStrictness == null) return false;
  if (store.drillStrictnessCp == null) return false;
  return true;
};

const ChessGame = ({ onOpenHistory }: ChessGameProps = {}) => {
  // Reconstruct Chess from store state so it stays in sync after remounts.
  // liveFen is authoritative; moveHistory is replayed only when consistent
  // (to preserve PGN). Falls back to liveFen if history diverges.
  const chess = useMemo(() => {
    const { liveFen, moveHistory } = useGameStore.getState();
    const replayed = new Chess();
    let historyValid = true;
    for (const move of moveHistory) {
      try {
        if (!replayed.move(move.san)) {
          historyValid = false;
          break;
        }
      } catch {
        historyValid = false;
        break;
      }
    }
    if (historyValid && replayed.fen() === liveFen) {
      return replayed;
    }
    return new Chess(liveFen);
  }, []);

  // Singleton analysis store — persists across remounts like the game store.
  const analysisStore = gameAnalysisStore;
  const coordinator = useGameAnalysisCoordinator();

  // --- Cross-boundary state from zustand store ---
  const fen = useGameStore((s) => s.liveFen);
  const boardOrientation = useGameStore((s) => s.boardOrientation);
  const setBoardOrientation = useGameStore((s) => s.setBoardOrientation);
  const playerColor = useGameStore((s) => s.playerColor);
  const playerColorChoice = useGameStore((s) => s.playerColorChoice);
  const engineElo = useGameStore((s) => s.engineElo);
  const setEngineElo = useGameStore((s) => s.setEngineElo);
  const moveHistory = useGameStore((s) => s.moveHistory);
  const viewIndex = useGameStore((s) => s.viewIndex); // null = viewing live position
  const setViewIndex = useGameStore((s) => s.setViewIndex);
  const {
    status: engineStatus,
    isThinking,
    evaluatePosition,
    resetEngine,
  } = useStockfishEngine();

  // Imperative-only — ChessGame does NOT subscribe to analysis state.
  // Analysis is delegated to the coordinator which survives route navigation.
  const markSkipped = useCallback(
    (moveIndex: number, requestId: string) =>
      coordinator.markSkipped(moveIndex, requestId),
    [coordinator],
  );
  const analyzeMove = useCallback(
    (fen: string, move: string, playerColor: 'white' | 'black', moveIndex?: number, legalMoveCount?: number) =>
      coordinator.analyzeMove(fen, move, playerColor, moveIndex, legalMoveCount),
    [coordinator],
  );

  const location = useLocation();
  const navigate = useNavigate();
  const [engineMessage, setEngineMessage] = useState<string | null>(null);
  const sessionId = useGameStore((s) => s.sessionId);
  const isGameActive = useGameStore((s) => s.isGameActive);
  const gameResult = useGameStore((s) => s.gameResult);
  const [isStartingGame, setIsStartingGame] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  // Returning from /drill-analysis with a valid identity-bound marker restores
  // the just-played drill with "Again" ready instead of the new-game popup
  // (g-65ve). Computed once on mount from the router marker + transient snapshot
  // + retained store; the start overlay seeds from the same result so the popup
  // never flashes before an effect can run. Marker consumption (below) does not
  // recompute or clear this — it is the accepted result, not the input.
  const [isReviewedDrillReturn, setIsReviewedDrillReturn] = useState(() =>
    isReviewedDrillReturnValid(
      location.state,
      useDrillAnalysisStore.getState().snapshot,
      useGameStore.getState(),
    ),
  );
  const [showStartOverlay, setShowStartOverlay] = useState(() => {
    const store = useGameStore.getState();
    // Reviewed-drill-return restores the just-played drill with "Again" ready
    // instead of the popup (g-65ve); it already seeds the overlay hidden.
    if (
      isReviewedDrillReturnValid(
        location.state,
        useDrillAnalysisStore.getState().snapshot,
        store,
      )
    ) {
      return false;
    }
    // A retained session — active OR already-ended — must not re-seed the popup
    // on remount. Seeding true and relying on the render gate (!isGameActive)
    // let the stale flag surface the popup the moment the game ended (g-yuvr).
    return store.sessionId === null;
  });
  const [blunderAlert, setBlunderAlert] = useState<BlunderAlert | null>(null);
  const [showFlash, setShowFlash] = useState(false);
  const [blunderReviewId, setBlunderReviewId] = useState<number | null>(null);
  const [blunderReviewSrs, setBlunderReviewSrs] =
    useState<TargetBlunderSrs | null>(null);
  const [resolvedReview, setResolvedReview] = useState<ResolvedReview | null>(null);
  const [blunderTargetFen, setBlunderTargetFen] = useState<string | null>(null);
  const [showGhostInfo, setShowGhostInfo] = useState(false);
  const ghostInfoAnchorRef = useRef<HTMLSpanElement>(null);
  const [, setShowPassToast] = useState(false);
  const [showRehookToast, setShowRehookToast] = useState(false);
  const [copyPositionNotice, setCopyPositionNotice] =
    useState<CopyPositionNotice | null>(null);
  const copyPositionNoticeNonceRef = useRef(0);
  const showCopyPositionNotice = useCallback(
    (kind: CopyPositionNotice["kind"]) => {
      copyPositionNoticeNonceRef.current += 1;
      setCopyPositionNotice({
        kind,
        nonce: copyPositionNoticeNonceRef.current,
      });
    },
    [],
  );
  const [reviewFailModal, setReviewFailModal] = useState<ReviewFailInfo | null>(
    null,
  );
  const [srsFailTrigger, setSrsFailTrigger] = useState<{
    id: number;
    moveIndex: number;
  } | null>(null);
  const srsFailNonceRef = useRef(0);
  // Dramatic win/loss/draw fanfare over the board (g-8079). Nonce trigger set by
  // the lifecycle's single genuine-end choke point (onGameFinished). Defined here
  // (above useChessGameLifecycle) so triggerEndGameFanfare is in scope when passed
  // in as onGameFinished.
  const [endGameFanfare, setEndGameFanfare] =
    useState<EndGameFanfareTrigger | null>(null);
  const endGameFanfareNonceRef = useRef(0);
  // Bump the nonce so the fanfare (re)starts cleanly; fired from the lifecycle's
  // single genuine-end choke point (onGameFinished), once per session (g-8079).
  const triggerEndGameFanfare = useCallback((result: GameResult) => {
    endGameFanfareNonceRef.current += 1;
    setEndGameFanfare({ id: endGameFanfareNonceRef.current, result });
  }, []);
  const handleEndGameFanfareDone = useCallback((id: number) => {
    setEndGameFanfare((prev) => (prev?.id === id ? null : prev));
  }, []);
  const [drillFailInfo, setDrillFailInfo] = useState<DrillFailInfo | null>(null);
  const [showPostGamePrompt, setShowPostGamePrompt] = useState(false);
  const [analysisMapSnapshot, setAnalysisMapSnapshot] = useState(
    () => analysisStore.getState().analysisMap,
  );
  const [perfectStreak, setPerfectStreak] = useState({
    current: 0,
    bestInHistory: 0,
    personalBest: 0,
  });
  const [streakToast, setStreakToast] = useState<PerfectStreakEvent | null>(
    null,
  );
  const perfectStreakPersonalBestRef = useRef(0);
  const perfectStreakRecordBaselineRef = useRef<number | null>(null);
  const [isPerfectStreakBaselineLoaded, setIsPerfectStreakBaselineLoaded] =
    useState(false);
  const previousPerfectStreakRef =
    useRef<PreviousPerfectStreakState | null>(null);
  const celebratedPerfectStreakKeysRef = useRef<Set<string>>(new Set());
  const isRated = useGameStore((s) => s.isRated);
  const isPracticeContinuation = useGameStore((s) => s.isPracticeContinuation);
  const [showRevertWarning, setShowRevertWarning] = useState(false);
  const [isRevertPendingState, setIsRevertPendingState] = useState(false);
  const [revertError, setRevertError] = useState<string | null>(null);
  const [showResignWarning, setShowResignWarning] = useState(false);
  const playerRating = useGameStore((s) => s.playerRating);
  const isProvisional = useGameStore((s) => s.isProvisional);
  const ratingScores = useGameStore((s) => s.ratingScores);
  const scoreChanges = useGameStore((s) => s.scoreChanges);
  const openingScoreDelta = useGameStore((s) => s.openingScoreDelta);
  const isDrillDeltaPending =
    openingScoreDelta?.sessionId === sessionId &&
    openingScoreDelta.freshness === "pending";
  // Inline badges render a delta ONLY for the session that earned it (g-f3m4).
  // A stale-stamped delta (its drill was replaced) renders nothing here; it is
  // surfaced as a last-drill toast instead.
  const openingScoreChanges = useMemo(
    () =>
      openingScoreDelta?.sessionId === sessionId ? openingScoreDelta.items : null,
    [openingScoreDelta, sessionId],
  );
  const ratingChange = useGameStore((s) => s.ratingChange);
  // A previous drill's diff that reconciled after the player moved on (g-f3m4).
  const { toast: lastDrillDeltaToast, dismiss: dismissLastDrillDelta } =
    useLastDrillDeltaToast();
  const [pendingPromotion, setPendingPromotion] = useState<{ from: string; to: string } | null>(null);
  const [selectedSquare, setSelectedSquare] = useState<string | null>(null);
  const [optionSquares, setOptionSquares] = useState<
    Record<string, React.CSSProperties>
  >({});
  const [boardInstanceKey, setBoardInstanceKey] = useState(0);
  const isRevertPendingRef = useRef(isRevertPendingState);
  const isPreparingAnalysisRef = useRef(false);
  const [isPreparingAnalysis, setIsPreparingAnalysis] = useState(false);
  // Error surfaced inside the stopped-drill actions (e.g. abandon failed).
  // engineMessage is hidden while isStoppedDrill, so this needs its own slot.
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  // Failed-move index for a stopped drill. Held in a ref so it survives history
  // navigation (which clears the transient drillFailInfo state) — the Analyze
  // barrier and snapshot still target the right ply afterwards.
  const drillFailedMoveIndexRef = useRef<number | null>(null);
  const setIsRevertPending = useCallback((update: SetStateAction<boolean>) => {
    const nextValue =
      typeof update === "function"
        ? (update as (prev: boolean) => boolean)(isRevertPendingRef.current)
        : update;
    isRevertPendingRef.current = nextValue;
    setIsRevertPendingState(nextValue);
  }, []);
  const isRevertPending = isRevertPendingState;

  // ---- Drill state --------------------------------------------------
  const [isDrillMode, setIsDrillMode] = useState(false);
  const [selectedDrillOpening, setSelectedDrillOpening] = useState<OpeningRootItem | null>(null);
  // Always null on every panel open (g-09mu force-always): no saved pref or
  // store value pre-selects a strictness tier — the user must consciously pick
  // one each time. The committed cp lives in the game store, not here.
  const [drillStrictnessCp, setDrillStrictnessCp] = useState<number | null>(null);
  // Non-committed seed for the start panel's difficulty (play + drill). The panel
  // drafts from this and commits to the store only on Start, so opening/cancelling
  // the popup never mutates the live engineElo (g-fxrm).
  const [seedEngineElo, setSeedEngineElo] = useState(
    () => useGameStore.getState().engineElo,
  );
  // Drill side is independent of normal-play's store playerColorChoice (which
  // also feeds normal-game random); drill never offers Random.
  const [drillPlayerColor, setDrillPlayerColor] = useState<"white" | "black">("white");
  const [openingFamilies, setOpeningFamilies] = useState<Array<{ family_name: string; roots: OpeningRootItem[] }> | null>(null);
  const [isLoadingOpenings, setIsLoadingOpenings] = useState(false);
  const [drillRecovery, setDrillRecovery] = useState<DrillRecovery | null>(null);
  // The drill root confirmation barrier, owned by useGameStore so it outlives a
  // remount the way the board it is about does (see the field's comment). NON-NULL
  // ⇒ engaged, covering both the in-flight and the failed case: a root-reaching
  // move has been applied but the backend has not confirmed it, so the drill is
  // NOT root-reached and no further gameplay may proceed.
  const rootConfirm = useGameStore((s) => s.drillRootConfirm);
  const setRootConfirmBarrier = useGameStore((s) => s.setDrillRootConfirm);
  // Its player-arrival counterpart: the applied move whose route-check has not
  // settled. Durable for the same reasons — see the store field's comment.
  const pendingRouteMove = useGameStore((s) => s.drillPendingRouteMove);
  const setPendingRouteMove = useGameStore((s) => s.setDrillPendingRouteMove);
  const pendingDrillSetupRef = useRef<{ openingKey: string; playerColor: string } | null>(null);
  // Set when handleAgainSettings seeds the setup panel from live store state, so
  // the localStorage prefill effect doesn't clobber the exact store values.
  const skipStickyPrefillRef = useRef(false);
  // Ad-hoc card drills (from /openings) carry their own UCI line + a synthetic
  // selection, so they must NOT depend on the getOpeningRoots() list. adHocLineRef
  // holds the line to send to startDrill (null → registered-root drill);
  // navColorRef guards the localStorage prefill from clobbering the nav color.
  const adHocLineRef = useRef<string[] | null>(null);
  const navColorRef = useRef(false);
  const drillOpeningKey = useGameStore((s) => s.drillOpeningKey);
  const drillOpeningName = useGameStore((s) => s.drillOpeningName);
  const drillState = useGameStore((s) => s.drillState);
  const isStoppedDrill = drillOpeningKey !== null && drillState === "failed";
  const isActiveDrill =
    drillOpeningKey !== null &&
    isGameActive &&
    (drillState === "active" || drillState === "root_reached");
  // A live drill can be replaced without affecting Elo. Converted drills are
  // rated games from the conversion point onward, so they keep the regular
  // live-game guard and cannot open a replacement from an opening card.
  const canStartDrillWhileGameActive =
    drillOpeningKey !== null &&
    drillState !== null &&
    drillState !== "converted" &&
    isGameActive;
  const drillTerminalReason = useGameStore((s) => s.drillTerminalReason);
  // -------------------------------------------------------------------

  // Recording/SRS decision state (blunderRecorded, contextMap, pendingSrsMap,
  // frontier, committedDecisionIndex) now lives on the coordinator-owned
  // DecisionOwner (g-2m0p), not on React refs.
  const moveMessagesRef = useRef<Map<number, MoveMessage[]>>(new Map());
  const [moveMessagesVersion, setMoveMessagesVersion] = useState(0);
  const previousOpponentModeRef = useRef<"ghost" | "engine" | null>(null);
  // Guards the opening opponent-move effect against re-entrancy: the effect is
  // async (backend round-trip) and its guard conditions stay true during the
  // await, so volatile deps re-running it would fire a second concurrent
  // request and apply an illegal move on an already-moved board.
  const openingOpponentMoveInFlightRef = useRef(false);
  const handleGameEndRef = useRef<() => Promise<void>>(async () => {});
  const blunderBoardTimerRefs = useRef<ReturnType<typeof setTimeout>[]>([]);
  const handleGameEndStable = useCallback(
    () => handleGameEndRef.current(),
    [],
  );

  const displayedFen = useMemo(() => {
    if (viewIndex === null) {
      return fen; // Live position
    }
    if (viewIndex === -1) {
      return STARTING_FEN; // Starting position
    }
    return moveHistory[viewIndex]?.fen ?? fen;
  }, [viewIndex, fen, moveHistory]);
  const handleCopyPosition = useCallback(() => {
    const clipboard = navigator.clipboard;
    if (!clipboard) {
      console.error("[Clipboard] Clipboard API is unavailable");
      showCopyPositionNotice("error");
      return;
    }
    void (async () => {
      try {
        await clipboard.writeText(displayedFen);
        showCopyPositionNotice("success");
      } catch (error) {
        console.error("[Clipboard] Failed to copy position FEN:", error);
        showCopyPositionNotice("error");
      }
    })();
  }, [displayedFen, showCopyPositionNotice]);
  const displayedIndex = useMemo(() => {
    if (viewIndex === null) {
      return moveHistory.length - 1;
    }
    return viewIndex;
  }, [moveHistory.length, viewIndex]);
  const displayedIndexRef = useRef(displayedIndex);
  displayedIndexRef.current = displayedIndex;
  const isBlunderBoardOverrideActive = blunderAlert !== null || drillFailInfo !== null;

  const clearBlunderBoardOverride = useCallback(() => {
    for (const timer of blunderBoardTimerRefs.current) {
      clearTimeout(timer);
    }
    blunderBoardTimerRefs.current = [];
  }, []);

  const lastMoveSquares = useMemo((): Record<string, React.CSSProperties> => {
    return deriveLastMoveSquares(moveHistory, viewIndex);
  }, [moveHistory, viewIndex]);

  // Compute arrows from review fail modal or blunder alert
  const blunderArrows = useMemo(() => {
    return deriveBlunderArrows(reviewFailModal, blunderAlert, drillFailInfo);
  }, [reviewFailModal, blunderAlert, drillFailInfo]);

  // Live opening-lineage hierarchy (broadest -> deepest), driven from the active
  // session. Local moves fetch immediately, then each fulfilled incremental
  // upload changes a coordinator-owned revision so enrichment is causally
  // ordered after durability instead of guessed with timers. The terminal flag
  // remains independent because final_full uploads do not use that channel.
  const uploadCommitRevision = useSessionUploadCommitRevision(
    coordinator,
    sessionId,
  );
  const openingLineageRefetchKey = JSON.stringify([
    moveHistory.length,
    Boolean(openingScoreChanges),
    uploadCommitRevision,
  ]);
  const {
    lineage: openingLineageFromServer,
    playerColor: openingLineagePlayerColor,
    startPly: openingLineageServerStartPly,
    scoreStatus: openingScoreStatus,
  } = useSessionOpenings(sessionId, {
    // A tuple-shaped primitive keeps independent invalidators collision-free:
    // a revert can shrink move count while an upload revision rises, and those
    // opposite changes must not cancel as they could under arithmetic addition.
    refetchKey: openingLineageRefetchKey,
  });

  // Display lineage: derived from LOCAL move history so a card renders on the
  // same tick as the move that crossed its root, with the server response
  // merged in for scores only (g-a5v3). Gating display on the server response
  // made cards appear seconds late — it is not causally ordered with the move.
  const {
    lineage: openingLineage,
    startPly: openingLineageLocalStartPly,
    pendingScoreIndices: openingLineagePendingScoreIndices,
  } = useLiveOpeningLineage(
    moveHistory,
    openingLineageFromServer,
    sessionId,
  );

  // Prefer the server's authoritative start ply once it has answered; the local
  // derivation covers the window before that (and matches it in every case we
  // can construct — see useLiveOpeningLineage.test.ts).
  const openingLineageStartPly =
    openingLineageFromServer.length > 0
      ? openingLineageServerStartPly
      : openingLineageLocalStartPly;

  // Whether the user can make moves (must be viewing live position)
  const isViewingLive = viewIndex === null;

  // During a LIVE game, washes the board when the user is reviewing a past move
  // (navigated back, or a blunder/drill-fail jumped backward). Uses displayedIndex
  // (latest normalized to last ply) rather than `viewIndex !== null` so a non-null
  // latest index — e.g. a graph click on the rightmost point — does NOT wash.
  const isReviewingPast = isGameActive && displayedIndex < moveHistory.length - 1;

  // Nonce bumped on a board interaction (click OR drag) while reviewing a past
  // move, so BoardStage can shake the board (g-1y68 A3). A monotonic counter
  // re-arms the shake on every attempt even when the reviewing state itself
  // hasn't changed.
  const [reviewNudge, setReviewNudge] = useState(0);
  const triggerReviewShake = useCallback(() => {
    setReviewNudge((n) => n + 1);
  }, []);

  const isPlayerMoveIndex = useCallback(
    (index: number) => {
      if (index < 0) return false;
      const isWhiteMove = index % 2 === 0;
      return playerColor === "white" ? isWhiteMove : !isWhiteMove;
    },
    [playerColor],
  );

  const clearMoveHighlights = useCallback(() => {
    setSelectedSquare(null);
    setOptionSquares({});
  }, []);

  const handleNavigate = useCallback(
    (index: number | null) => {
      if (isRevertPendingRef.current) {
        return;
      }
      setViewIndex(index);
      setReviewFailModal(null);
      if (pendingPromotion) {
        setPendingPromotion(null);
        clearMoveHighlights();
      }

      // Re-show blunder alert when clicking on a player's blunder move
      if (index !== null && index >= 0) {
        const { analysisMap } = analysisStore.getState();
        const history = useGameStore.getState().moveHistory;
        const analysis = analysisMap.get(index);
        if (
          analysis?.blunder &&
          analysis.delta !== null &&
          isPlayerMoveIndex(index)
        ) {
          const moveSan = history[index]?.san ?? analysis.move;
          setBlunderAlert(
            buildBlunderAlert({
              moveHistory: history,
              moveIndex: index,
              moveSan,
              moveUci: analysis.move,
              bestMoveUci: analysis.bestMove,
              delta: analysis.delta,
            }),
          );
          return;
        }
      }

      // Clear blunder alert when navigating to a non-blunder move
      clearBlunderBoardOverride();
      setBlunderAlert(null);
      setDrillFailInfo(null);
    },
    [analysisStore, clearBlunderBoardOverride, clearMoveHighlights, isPlayerMoveIndex, pendingPromotion, setViewIndex],
  );

  // Return-to-live for the floating board pill (g-1y68 A1) — the same path the
  // move list's ⟩⟩ button uses. Firing it nulls viewIndex, which flips
  // isReviewingPast false and unmounts every reviewing cue in one commit.
  const handleReturnToLive = useCallback(() => {
    handleNavigate(null);
  }, [handleNavigate]);

  const getMoveOptions = useCallback(
    (square: string): boolean => {
      if (!isSquare(square)) {
        return false;
      }

      const moves = chess.moves({ square, verbose: true });
      if (moves.length === 0) {
        return false;
      }

      const sourcePiece = chess.get(square);
      const newSquares: Record<string, React.CSSProperties> = {};
      for (const move of moves) {
        const target = chess.get(move.to);
        const isCapture =
          sourcePiece != null &&
          target != null &&
          target.color !== sourcePiece.color;
        newSquares[move.to] = {
          background: isCapture
            ? "rgba(255, 0, 0, 0.4)"
            : "radial-gradient(circle, rgba(0,0,0,.1) 25%, transparent 25%)",
          borderRadius: "50%",
        };
      }

      newSquares[square] = {
        background: "rgba(255, 255, 0, 0.4)",
      };

      setOptionSquares(newSquares);
      return true;
    },
    [chess],
  );

  const isPlayersTurn = chess.turn() === (playerColor === "white" ? "w" : "b");
  const moveCount = moveHistory.length;
  const isReviewMomentActive =
    hasReviewTargetAtFen(blunderReviewId, blunderTargetFen, fen) &&
    isGameActive &&
    isPlayersTurn &&
    isViewingLive &&
    !chess.isGameOver();
  const blocksStreakToast =
    (showStartOverlay &&
      (!isGameActive || isStoppedDrill || canStartDrillWhileGameActive)) ||
    showRevertWarning ||
    showResignWarning ||
    pendingPromotion !== null ||
    blunderAlert !== null ||
    isReviewMomentActive ||
    resolvedReview !== null ||
    showRehookToast;

  const statusText = deriveStatusText(chess);

  const appendMoveMessage = useCallback(
    (moveIndex: number, msg: MoveMessage) => {
      const map = moveMessagesRef.current;
      const existing = map.get(moveIndex);
      if (existing) {
        existing.push(msg);
      } else {
        map.set(moveIndex, [msg]);
      }
      setMoveMessagesVersion((v) => v + 1);
    },
    [],
  );

  // Build a stable snapshot for passing to ConnectedMoveList.
  // Preserves per-index array references when that index's messages haven't changed,
  // so MoveRow memoization can skip unchanged rows.
  // Invariant: appendMoveMessage only pushes (never edits in place at constant length).
  // A future "replace message" path would need to invalidate differently.
  const prevMoveMessagesSnapshotRef = useRef<ReadonlyMap<number, MoveMessage[]>>(new Map());

  const moveMessages = useMemo(() => {
    void moveMessagesVersion; // depend on version counter
    const prev = prevMoveMessagesSnapshotRef.current;
    const next = new Map<number, MoveMessage[]>();
    for (const [moveIndex, messages] of moveMessagesRef.current) {
      const prevArr = prev.get(moveIndex);
      if (prevArr && prevArr.length === messages.length) {
        // No new messages appended — reuse previous array reference
        next.set(moveIndex, prevArr);
      } else {
        next.set(moveIndex, [...messages]);
      }
    }
    const result = next as ReadonlyMap<number, MoveMessage[]>;
    prevMoveMessagesSnapshotRef.current = result;
    return result;
  }, [moveMessagesVersion]);

  const opponentColor = playerColor === "white" ? "black" : "white";

  /**
   * Confirm an applied root-reaching move against the position the backend
   * recorded, and transition the drill only if it agrees.
   *
   * Serving a route move is no longer a transition — the drill becomes
   * root_reached here, or not at all. Every exit path re-checks
   * `isStillCurrentRootConfirm` because this awaits a network round trip that a
   * revert, an abandon, or a new game can outlive.
   */
  const confirmDrillRoot = useCallback(
    async (request: DrillRootConfirmRequest): Promise<boolean> => {
      // A fresh object per attempt, so its REFERENCE is this attempt's ownership
      // token for the barrier. Retry and the remount resume both re-submit the
      // same `request`, so an attempt must never treat the request's own identity
      // as proof of ownership.
      const claim: DrillRootConfirmRequest = { ...request };
      // Synchronous, before any await: the barrier must be engaged by the time
      // this function yields, or a drop between the serve and the first await
      // would slip through applyPlayerMove's guard.
      setRootConfirmBarrier(claim);
      // Clearing here is what makes every attempt look the same, retries
      // included. Left set, a retry would keep the Retry button mounted for the
      // whole second request and invite a concurrent confirmation on top of the
      // in-flight one. In flight ⇒ no recovery ⇒ Abandon only.
      setDrillRecovery((current) =>
        current?.kind === "root-confirm" ? null : current,
      );
      setEngineMessage("Confirming the opening root…");

      // Attempt ownership, checked before ANY write on a late response. A
      // confirmation can outlive an abandon, and the drill that replaces it can
      // engage a barrier of its own; without this check the older attempt would
      // settle stale and clear the NEWER attempt's barrier, re-enabling the board
      // while that confirmation is still unresolved. An attempt that no longer
      // owns the barrier is discarded whole — including a successful answer,
      // which the owning attempt will obtain for itself.
      const ownsBarrier = () =>
        useGameStore.getState().drillRootConfirm === claim;

      // Identity, not shape. Session id + history length + "not reverting" all
      // still match after abandoning the drill, and after replaying a DIFFERENT
      // branch back to the same ply — a late response would then stamp
      // root_reached onto an abandoned or non-root position.
      const isStillCurrentRootConfirm = () => {
        const current = useGameStore.getState();
        return (
          current.sessionId === request.sessionId &&
          current.isGameActive &&
          current.drillOpeningKey !== null &&
          current.drillState === "active" &&
          current.moveHistory.length === request.ply &&
          current.moveHistory[request.ply - 1]?.uci === request.uci &&
          chess.fen() === request.fen &&
          !isRevertPendingRef.current
        );
      };

      try {
        const route = await checkDrillRoute(
          request.sessionId,
          {
            current_fen: request.fen,
            current_ply: request.ply,
            ...(request.decisionId ? { decision_id: request.decisionId } : {}),
          },
          { signal: AbortSignal.timeout(ROUTE_CHECK_TIMEOUT_MS) },
        );
        if (!ownsBarrier()) {
          return false;
        }
        if (!isStillCurrentRootConfirm()) {
          setRootConfirmBarrier(null);
          return false;
        }
        if (route.status === "root_reached") {
          setRootConfirmBarrier(null);
          useGameStore.getState().setDrillState("root_reached");
          setDrillRecovery(null);
          setEngineMessage("Opening root reached. Drill is live.");
          return true;
        }
        // Anything else is a confirmation the backend would not make. Keep the
        // barrier engaged and offer recovery; the applied board is untouched.
        setDrillRecovery({ kind: "root-confirm", request });
        setEngineMessage(
          "Could not confirm the opening root. Try again or abandon the drill.",
        );
        return false;
      } catch (error) {
        if (!ownsBarrier()) {
          return false;
        }
        if (!isStillCurrentRootConfirm()) {
          setRootConfirmBarrier(null);
          return false;
        }
        setDrillRecovery({ kind: "root-confirm", request });
        setEngineMessage(
          error instanceof Error && error.name === "TimeoutError"
            ? "Confirming the opening root timed out. Try again or abandon the drill."
            : "Could not confirm the opening root. Try again or abandon the drill.",
        );
        return false;
      }
    },
    [chess, setEngineMessage, setRootConfirmBarrier],
  );

  const isDrillRootConfirmPending = useCallback(
    () => useGameStore.getState().drillRootConfirm !== null,
    [],
  );

  // The barrier's invariant: it may only be engaged while the exact position it is
  // about is still on the board. This clears it on revert, new game, reset and
  // resign without any of those paths having to know it exists. A late response
  // that arrives afterwards is separately rejected by isStillCurrentRootConfirm.
  useEffect(() => {
    if (rootConfirm && (!isGameActive || moveHistory.length !== rootConfirm.ply)) {
      setRootConfirmBarrier(null);
      setDrillRecovery((current) =>
        current?.kind === "root-confirm" ? null : current,
      );
    }
  }, [isGameActive, moveHistory.length, rootConfirm, setRootConfirmBarrier]);

  // The same invariant for the player-arrival confirmation: a pending route move is
  // only pending while that exact move is still live history. A revert-and-replace
  // must drop it, or the Retry it keeps mounted would submit a proof for a move no
  // longer on the board — which the backend can prove and stamp regardless.
  useEffect(() => {
    if (
      pendingRouteMove &&
      (!isGameActive ||
        moveHistory[pendingRouteMove.moveIndex]?.uci !== pendingRouteMove.moveUci)
    ) {
      setPendingRouteMove(null);
      setDrillRecovery((current) =>
        current?.kind === "player-route" ? null : current,
      );
    }
  }, [isGameActive, moveHistory, pendingRouteMove, setPendingRouteMove]);

  const { applyPlayerMove, handleDrop, applyEngineMove, applyGhostMove } =
    useChessGameController({
      chess,
      blunderReviewId,
      blunderReviewSrs,
      blunderTargetFen,
      decisionOwner: coordinator.decisionOwner,
      markSkipped,
      setEngineMessage,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setShowGhostInfo,
      resolvedReview,
      setResolvedReview,
      analyzeMove,
      evaluatePosition,
      handleGameEnd: handleGameEndStable,
      clearMoveHighlights,
      clearBlunderBoardOverride,
      confirmDrillRoot,
      isDrillRootConfirmPending,
    });

  const { opponentMode, applyOpponentMove, resetMode } = useOpponentMove({
    sessionId,
    canApplyResult: (requestSessionId) => {
      const store = useGameStore.getState();
      return (
        store.isGameActive &&
        store.sessionId === requestSessionId &&
        store.drillState !== "failed" &&
        !isRevertPendingRef.current &&
        // An unconfirmed root is not a root: no opponent move may be applied on
        // top of a position the drill has not yet been proven to have reached.
        store.drillRootConfirm === null
      );
    },
    onApplyBackendMove: async (...args) => {
      if (useGameStore.getState().isGameActive && !isRevertPendingRef.current) {
        await applyGhostMove(...args);
      }
    },
    onApplyLocalFallback: async () => {
      if (useGameStore.getState().isGameActive && !isRevertPendingRef.current) {
        await applyEngineMove();
      }
    },
    shouldUseLocalFallback: () => {
      const store = useGameStore.getState();
      return !(store.drillOpeningKey && (store.drillState === "active" || store.drillState === "root_reached"));
    },
    onBackendFailure: async () => {
      const store = useGameStore.getState();
      if (store.drillOpeningKey && store.drillState === "root_reached") {
        setDrillRecovery({
          kind: "opponent",
          fen: chess.fen(),
          uciHistory: store.moveHistory.map((move) => move.uci),
        });
        setEngineMessage("Opponent move is unavailable. Try again or abandon the drill.");
        return;
      }
      setEngineMessage("Drill steering is unavailable. Try again or abandon the drill.");
    },
  });

  // The rehook signal is pre-gated here so useBoardNotice only sees a rising
  // edge it should surface (mirrors the old warning-stack guard).
  const showRehookNotice =
    isGameActive && opponentMode === "ghost" && showRehookToast;
  const boardNotice = useBoardNotice({
    isReviewMomentActive,
    resolvedReview,
    showRehookNotice,
    isViewingLive,
  });

  const checkPostPlayerDrillRoute = useCallback(
    async (result: Extract<PlayerMoveApplyResult, { applied: true }>) => {
      const store = useGameStore.getState();
      if (!store.sessionId || !store.drillOpeningKey || store.drillState !== "active") {
        return true;
      }
      const requestSessionId = store.sessionId;
      // Identity, not shape — and the ply this request CLAIMS (`current_ply` below
      // is `uciHistory.length`) must still be the live ply. The played move's uci
      // at its own index is not enough on its own: it still matches after the game
      // moved on past it, which is exactly what a concurrent attempt does when it
      // completes first and appends the opponent's reply.
      const isStillCurrentRouteCheck = () => {
        const current = useGameStore.getState();
        const live = current.moveHistory[result.moveIndex];
        return (
          current.sessionId === requestSessionId &&
          current.moveHistory.length === result.uciHistory.length &&
          live?.uci === result.moveUci &&
          live?.fen === result.fenAfter &&
          current.drillOpeningKey !== null &&
          current.drillState === "active" &&
          !isRevertPendingRef.current
        );
      };

      // Checked BEFORE dispatch, not only after. A retry re-submits a move the
      // player may since have reverted and replaced, and AT the root the backend
      // would replay `previous_fen` + `played_uci`, prove that arrival, and stamp
      // state and boundary for a move no longer on the board — something no
      // post-await guard can undo. The invariant effect on `drillPendingRouteMove`
      // disarms every retry path that can reach here today, so this is the last
      // line rather than the first: it survives that effect's dependency list or
      // declaration order being changed by someone who does not know about this.
      if (!isStillCurrentRouteCheck()) {
        return false;
      }
      // Durable record of the pending confirmation, cleared on every settled
      // outcome below. It survives a remount so the interrupted continuation can be
      // resumed instead of stranding the drill with the opponent to move.
      store.setDrillPendingRouteMove(result);

      // Attempt ownership, checked before ANY write on a late response — the same
      // whole-response rule the root confirmation uses. `result`'s REFERENCE is this
      // attempt's token: the remount resume and Retry both spread the durable record
      // into a fresh object, so a second attempt for the same move owns a different
      // token. Without this, an attempt whose component unmounted mid-flight still
      // passes every identity field and goes on to request and apply an opponent move
      // through its own dead Chess instance — while the resumed attempt does the same,
      // appending twice to the one shared history. An attempt that no longer owns the
      // record is discarded whole, successful answers included; the owning attempt
      // obtains its own.
      const ownsPending = () =>
        useGameStore.getState().drillPendingRouteMove === result;
      // Unconditional: every call site below is gated on ownsPending() with no await
      // in between, so one ownership rule governs the whole response.
      const releasePending = () =>
        useGameStore.getState().setDrillPendingRouteMove(null);

      try {
        // current_ply is ordinary metadata away from the root; AT the root it is a
        // boundary claim, and this call becomes the confirmation for drills where
        // the PLAYER moves into the root. decision_id is deliberately absent —
        // the backend rejects it on a player arrival and proves the ply from
        // previous_fen + played_uci instead.
        //
        // Bounded for the same reason the opponent-arrival confirmation is: the
        // drill cannot advance until this settles, and the Retry that re-enters
        // here drops the recovery banner for the duration. Unbounded, a request
        // that never settles would strand the committed move with no message and
        // no way out.
        const route = await checkDrillRoute(
          requestSessionId,
          {
            current_fen: result.fenAfter,
            previous_fen: result.fenBefore,
            played_uci: result.moveUci,
            current_ply: result.uciHistory.length,
          },
          { signal: AbortSignal.timeout(ROUTE_CHECK_TIMEOUT_MS) },
        );
        if (!ownsPending()) {
          return false;
        }
        releasePending();
        if (!isStillCurrentRouteCheck()) {
          return false;
        }
        if (route.status === "failed") {
          setDrillRecovery(null);
          const reason = route.failure?.reason ?? null;
          useGameStore.getState().setDrillState("failed");
          useGameStore.getState().setDrillTerminalReason(reason);
          // No opening-score delta on an off-route fail: route-check is a
          // speculative per-move call, so we can't run the full-history upload
          // barrier before the backend reads session_moves, and going off-route
          // means the target opening was never reached. Clear any prior value so
          // DrillStopActions shows no (stale) delta.
          // Clear the CURRENT slot only — a queued late notification from a
          // previous drill is owned by that drill and must survive (g-f3m4).
          useGameStore.getState().clearOpeningDelta();
          drillFailedMoveIndexRef.current = result.moveIndex;
          setDrillFailInfo({
            playedMoveUci: route.failure?.played_move_uci ?? result.moveUci,
            suggestionUcis: route.suggestions.map((suggestion) => suggestion.uci),
            correctionFen: route.failure?.correction_fen ?? result.fenBefore,
            moveIndex: result.moveIndex,
          });
          setEngineMessage(
            route.failure?.reason === "accuracy"
              ? "Bad move"
              : "That's not how you get to the opening.",
          );
          setViewIndex(result.moveIndex - 1);
          return false;
        }
        if (route.status === "root_reached") {
          setDrillFailInfo(null);
          setDrillRecovery(null);
          useGameStore.getState().setDrillState("root_reached");
          setEngineMessage("Opening root reached. Drill is live.");
          return true;
        }
        setDrillFailInfo(null);
        setDrillRecovery(null);
        return true;
      } catch (error) {
        if (!ownsPending()) {
          return false;
        }
        if (!isStillCurrentRouteCheck()) {
          releasePending();
          return false;
        }
        // This call can BE a boundary stamp (a player arrival at the root), so a
        // failure must be retryable as itself. Without a recovery the Retry button
        // falls through to applyOpponentMove and the drill advances with the
        // boundary never stamped — the exact outcome the two-phase transition
        // exists to prevent.
        // The pending record deliberately survives — it IS the retryable work.
        setDrillRecovery({ kind: "player-route" });
        const message =
          error instanceof Error && error.name === "TimeoutError"
            ? "Checking the opening route timed out."
            : error instanceof Error
              ? error.message
              : "Failed to check drill route.";
        setEngineMessage(`${message} Try again or abandon the drill.`);
        return false;
      }
    },
    [setEngineMessage, setViewIndex],
  );

  const {
    handleGameEnd,
    executeRevert,
    handleRevertClick,
    cancelRevert,
    handleNewGame,
    handleNewDrill,
    handleResignClick,
    executeResign,
    cancelResign,
    handleReset,
    handleShowStartOverlay,
    handleViewAnalysis,
    abandonStoppedDrill,
    uploadFullMoveHistoryBeforeEnd,
  } = useChessGameLifecycle({
    chess,
    coordinator,
    clearMoveHighlights,
    resetMode,
    resetEngine,
    onOpenHistory,
    setEngineMessage,
    setIsStartingGame,
    setStartError,
    setShowStartOverlay,
    setSeedEngineElo,
    setBlunderAlert,
    setShowFlash,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setShowPassToast,
    setShowRehookToast,
    setReviewFailModal,
    setShowPostGamePrompt,
    setIsRevertPending,
    setRevertError,
    showRevertWarning,
    setShowRevertWarning,
    setShowResignWarning,
    setResolvedReview,
    setPendingPromotion,
    clearBlunderBoardOverride,
    clearReviewedDrillReturn: () => setIsReviewedDrillReturn(false),
    onGameFinished: triggerEndGameFanfare,
  });

  // "Analyze" drill-end action (g-a406): snapshot the just-played drill while
  // ChessGame + AnalysisEffects are still mounted, flush blunder/SRS/evidence
  // side effects BEFORE navigation, abandon the drill (unrated, hidden), then
  // open the ephemeral /drill-analysis surface. No conversion, rating, or
  // history entry is created.
  const handleAnalyzeDrill = useCallback(async () => {
    if (isPreparingAnalysisRef.current) {
      return;
    }
    isPreparingAnalysisRef.current = true;
    setIsPreparingAnalysis(true);
    setAnalyzeError(null);
    // Pin the session this Analyze refers to. If the user starts a new drill
    // mid-preparation, every awaited step bails out before snapshotting or
    // abandoning the wrong session.
    const originalSessionId = useGameStore.getState().sessionId;
    const isStillOriginalSession = () =>
      useGameStore.getState().sessionId === originalSessionId;
    try {
      const failedMoveIndex = drillFailedMoveIndexRef.current;
      let warning: string | null = null;

      // Targeted barrier: only the off-route failed move may still be pending.
      if (
        failedMoveIndex !== null &&
        !analysisStore.getState().analysisMap.has(failedMoveIndex)
      ) {
        try {
          await coordinator.waitForAnalysis(failedMoveIndex);
          // Recording is coordinator-owned (g-2m0p) and runs regardless of
          // AnalysisEffects mount, but yield a frame so the resolved outcome's
          // synchronous recording decision + outbox enqueue lands before navigation.
          await new Promise((resolve) =>
            requestAnimationFrame(() => resolve(null)),
          );
        } catch {
          warning = "Analysis unavailable; showing partial review.";
        }
        if (!isStillOriginalSession()) return;
      }

      // Flush normal evidence uploads before unmounting AnalysisEffects.
      await coordinator.flushPendingUploads().catch((err) =>
        console.error("[Drill] flushPendingUploads failed:", err),
      );
      if (!isStillOriginalSession()) return;

      // Finalize the stopped drill: unrated, hidden, game inactive. If the
      // backend abandon fails, abandonStoppedDrill throws — keep the drill
      // active locally (do not clear/navigate) so cleanup can be retried.
      try {
        await abandonStoppedDrill();
      } catch (err) {
        console.error("[Drill] abandonStoppedDrill failed:", err);
        setAnalyzeError("Couldn't end the drill. Try Analyze again.");
        return;
      }
      if (!isStillOriginalSession()) return;

      const store = useGameStore.getState();
      // Bind the snapshot to the exact drill session it describes. The session
      // is still present in the store post-abandon (only isGameActive/drillState
      // changed), so this is the identity used to validate a later return.
      if (!store.sessionId) return;
      const snapshot = buildDrillAnalysisSnapshot(
        store.moveHistory,
        analysisStore.getState().analysisMap,
        STARTING_FEN,
        playerColor,
        failedMoveIndex,
        store.sessionId,
      );
      useDrillAnalysisStore.getState().setSnapshot({ ...snapshot, warning });

      // Let the live analysis session idle-shut down; snapshot is already copied.
      coordinator.clearSession();

      navigate("/drill-analysis");
    } finally {
      isPreparingAnalysisRef.current = false;
      setIsPreparingAnalysis(false);
    }
  }, [
    abandonStoppedDrill,
    analysisStore,
    coordinator,
    navigate,
    playerColor,
  ]);

  useEffect(() => {
    handleGameEndRef.current = handleGameEnd;
  }, [handleGameEnd]);

  useEffect(() => {
    const unsubscribe = analysisStore.subscribe((state, previous) => {
      if (state.analysisMap !== previous.analysisMap) {
        setAnalysisMapSnapshot(state.analysisMap);
      }
    });
    return unsubscribe;
  }, [analysisStore]);

  useEffect(() => {
    let cancelled = false;
    void getStatsAchievements()
      .then((achievements) => {
        if (cancelled) return;
        const nextBest = achievements.perfect_streak.personal_best;
        perfectStreakRecordBaselineRef.current = nextBest;
        perfectStreakPersonalBestRef.current = Math.max(
          perfectStreakPersonalBestRef.current,
          nextBest,
        );
        setPerfectStreak((current) => ({
          ...current,
          personalBest: Math.max(current.personalBest, nextBest),
        }));
        setIsPerfectStreakBaselineLoaded(true);
      })
      .catch(() => {
        // The live streak remains useful even if the all-time best is unavailable.
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (moveHistory.length === 0) {
      previousPerfectStreakRef.current = null;
      celebratedPerfectStreakKeysRef.current = new Set();
      setStreakToast(null);
      setPerfectStreak((current) => ({
        current: 0,
        bestInHistory: 0,
        personalBest: Math.max(
          current.personalBest,
          perfectStreakPersonalBestRef.current,
        ),
      }));
      return;
    }

    const result = derivePerfectStreak({
      moveHistory,
      analysisMap: analysisMapSnapshot,
      playerColor,
      previousPersonalBest: perfectStreakPersonalBestRef.current,
      recordPersonalBest: isPerfectStreakBaselineLoaded
        ? perfectStreakRecordBaselineRef.current
        : null,
      previousState: previousPerfectStreakRef.current,
      celebratedEventKeys: celebratedPerfectStreakKeysRef.current,
    });

    previousPerfectStreakRef.current = {
      current: result.current,
      bestInHistory: result.bestInHistory,
      personalBest: result.personalBest,
      recordPersonalBest: isPerfectStreakBaselineLoaded
        ? perfectStreakRecordBaselineRef.current
        : null,
    };
    perfectStreakPersonalBestRef.current = result.personalBest;
    setPerfectStreak((current) =>
      current.current === result.current &&
      current.bestInHistory === result.bestInHistory &&
      current.personalBest === result.personalBest
        ? current
        : {
            current: result.current,
            bestInHistory: result.bestInHistory,
            personalBest: result.personalBest,
          },
    );

    if (result.event) {
      celebratedPerfectStreakKeysRef.current.add(result.event.key);
      if (
        !blocksStreakToast &&
        (result.event.type === "record" || result.event.streak >= 5)
      ) {
        setStreakToast(result.event);
      }
    }
  }, [
    analysisMapSnapshot,
    blocksStreakToast,
    isPerfectStreakBaselineLoaded,
    moveHistory,
    playerColor,
  ]);

  // Sync coordinator with existing active session on mount (e.g., after refresh)
  useEffect(() => {
    const { sessionId: sid, isGameActive: active } = useGameStore.getState();
    if (sid && active && coordinator.sessionId !== sid) {
      coordinator.startSession(sid);
    }
  }, [coordinator]);

  // ---- Drill effects ------------------------------------------------
  // Consume the one-shot "return from drill analysis" marker so a refresh/back
  // can't re-trigger it. This only clears the router input — isReviewedDrillReturn
  // was already decided synchronously on mount and is NOT recomputed here.
  useEffect(() => {
    const marker = (location.state as ReturnFromDrillAnalysisMarker | null)
      ?.returnFromDrillAnalysis;
    if (!marker) return;
    navigate(location.pathname, { replace: true, state: null });
  }, [location.state, navigate, location.pathname]);

  // Intercept location.state from /openings navigation
  useEffect(() => {
    const drillSetup = (
      location.state as {
        drillSetup?: {
          openingKey?: string;
          targetFen?: string;
          line?: string[];
          displayName?: string | null;
          eco?: string | null;
          playerColor: string;
        };
      } | null
    )?.drillSetup;
    if (!drillSetup) return;

    setIsDrillMode(true);

    if (drillSetup.targetFen) {
      // Ad-hoc card drill: everything needed is in the nav state, so preselect
      // synthetically and DON'T wait for getOpeningRoots(). The roots list may be
      // loading or fail — neither must block this drill (opening_family is left
      // empty; the backend synthesizes display metadata from the line).
      setSelectedDrillOpening({
        opening_key: drillSetup.targetFen,
        opening_name: drillSetup.displayName ?? "Custom line",
        opening_family: "",
        eco: drillSetup.eco ?? null,
        depth: drillSetup.line?.length ?? 0,
      });
      adHocLineRef.current = drillSetup.line ?? [];
      setDrillPlayerColor(drillSetup.playerColor === "black" ? "black" : "white");
      navColorRef.current = true;
      // Fully handled here — keep the roots-match effect from touching this.
      pendingDrillSetupRef.current = null;
    } else {
      // Legacy registered-root path: defer selection to the roots-match effect.
      adHocLineRef.current = null;
      pendingDrillSetupRef.current = {
        openingKey: drillSetup.openingKey ?? "",
        playerColor: drillSetup.playerColor,
      };
    }

    setShowStartOverlay(true);

    navigate(location.pathname, { replace: true, state: null });

  }, [location.state, navigate, location.pathname]);

  // Fetch opening roots when drill mode active and overlay shown
  useEffect(() => {
    if (!showStartOverlay || !isDrillMode) return;
    let cancelled = false;
    setIsLoadingOpenings(true);
    getOpeningRoots()
      .then((data) => {
        if (cancelled) return;
        setOpeningFamilies(data.families);
      })
      .catch(() => {
        if (cancelled) return;
        setOpeningFamilies(null);
        // Drop any prior selection so a failed reload can't silently start a
        // stale opening behind the "Failed to load openings" trigger — but NOT
        // an ad-hoc card selection, which carries everything it needs (line +
        // synthesized metadata) and is startable without the roots list.
        if (adHocLineRef.current == null) {
          setSelectedDrillOpening(null);
        }
      })
      .finally(() => {
        if (cancelled) return;
        setIsLoadingOpenings(false);
      });
    return () => { cancelled = true; };
  }, [showStartOverlay, isDrillMode]);

  // Load sticky drill prefs when overlay opens
  useEffect(() => {
    if (!showStartOverlay) return;
    // An ad-hoc nav drill already applied its color (intercept runs before this
    // prefill on the overlay-open commit); don't let sticky prefs override it.
    const skipNavColor = navColorRef.current;
    navColorRef.current = false;
    // handleAgainSettings already seeded the panel from live store state; don't
    // let localStorage clobber those values.
    if (skipStickyPrefillRef.current) {
      skipStickyPrefillRef.current = false;
      return;
    }
    try {
      const raw = localStorage.getItem("ghostreplay_drill_prefs");
      if (!raw) return;
      const prefs = JSON.parse(raw);
      // Strictness is never prefilled (g-09mu force-always): the panel opens
      // with no tier selected so the user makes a conscious choice every time.
      // Difficulty is NOT seeded here: it is always a fresh sample near the
      // player's rating (g-ncvm), set by the idle rating fetch / New Game / gear.
      // Loading the stored prefs.engineElo would clobber that sample (g-fxrm).
      if (!skipNavColor) {
        if (prefs.playerColor === "white" || prefs.playerColor === "black") {
          setDrillPlayerColor(prefs.playerColor);
        } else if (prefs.playerColor === "random") {
          // Legacy stored drill pref: coerce dropped "random" to White.
          setDrillPlayerColor("white");
        }
      }
      // Don't seed a registered-root pending setup while an ad-hoc drill is the
      // active selection — its synthetic selection must not be overridden by the
      // roots-match effect.
      if (prefs.openingKey && !pendingDrillSetupRef.current && adHocLineRef.current == null) {
        pendingDrillSetupRef.current = {
          openingKey: prefs.openingKey,
          playerColor: prefs.playerColor ?? "random",
        };
      }
    } catch {
      // ignore corrupted storage
    }
  }, [showStartOverlay]);

  // Match pending drill setup after openingFamilies loads
  useEffect(() => {
    // Ad-hoc card drills are fully resolved in the intercept effect; the
    // roots-match path is for the registered-root flow only.
    if (adHocLineRef.current != null) return;
    if (!openingFamilies || !pendingDrillSetupRef.current) return;
    const opening = openingFamilies
      .flatMap((f) => f.roots)
      .find((r) => r.opening_key === pendingDrillSetupRef.current?.openingKey);
    if (opening) {
      setSelectedDrillOpening(opening);
    }
    const color = pendingDrillSetupRef.current.playerColor;
    setDrillPlayerColor(color === "black" ? "black" : "white");
    pendingDrillSetupRef.current = null;
  }, [openingFamilies]);
  // -------------------------------------------------------------------

  // Opening-lineage actions on /play (history parity).

  // Board navigation: jump the board to review the opening's position, mirroring
  // /history's handleSelectRoot. `item.moves` is the played SAN prefix up to and
  // INCLUDING the crossing move, so its last index is that move's index in the
  // session's move list (== moveHistory order) — a per-crossing index, so a
  // repeated opening root jumps to ITS crossing, not a first FEN match. Wired
  // during play AND post-game: it only reviews a past position (viewIndex), never
  // disturbing the live game.
  const handleLineageSelectRoot = useCallback(
    (item: OpeningLineageItem) => {
      const idx = item.moves.length - 1;
      if (idx >= 0 && idx < moveHistory.length) {
        handleNavigate(idx);
      }
    },
    [moveHistory, handleNavigate],
  );

  // Start Drill: mirror the /openings route-state intercept flow rather than
  // resolving openingFamilies directly — that list can be null until the
  // overlay opens it (the fetch effect runs only when showStartOverlay &&
  // isDrillMode). Seeding the pending setup + opening the overlay triggers the
  // load; the roots-match effect then resolves pendingDrillSetupRef ->
  // setSelectedDrillOpening. Not handleStartDrill (needs a full draft) or
  // handleShowStartOverlay alone (doesn't set drill mode / seed the ref).
  const handleLineageStartDrill = useCallback(
    (item: OpeningLineageItem) => {
      setIsDrillMode(true);
      // Fail closed while the requested registered root resolves. This state is
      // retained after a successful start, so leaving it intact would let the
      // newly-mounted panel submit a prior registered/ad-hoc opening before the
      // async roots fetch installs this card's selection.
      setSelectedDrillOpening(null);
      adHocLineRef.current = null;
      pendingDrillSetupRef.current = {
        openingKey: item.opening_key,
        playerColor,
      };
      setShowStartOverlay(true);
    },
    [playerColor],
  );

  const isPostRootMoveStillCurrent = useCallback(
    (
      capturedSessionId: string,
      result: Extract<PlayerMoveApplyResult, { applied: true }>,
    ) => {
      const store = useGameStore.getState();
      return (
        store.sessionId === capturedSessionId &&
        store.moveHistory[result.moveIndex]?.uci === result.moveUci &&
        store.drillState === "root_reached" &&
        !isRevertPendingRef.current
      );
    },
    [],
  );

  const stopPostRootDrillForAccuracy = useCallback(
    async (
      sessionId: string,
      result: Extract<PlayerMoveApplyResult, { applied: true }>,
      bestMove: string | null,
    ) => {
      // Durably upload the full move history before failDrill computes the
      // opening-score delta, so the recompute sees this drill's complete chain
      // (mirrors the natural-end barrier). Bounded; degrades on timeout.
      await uploadFullMoveHistoryBeforeEnd(sessionId, "accuracy_fail");
      const contract = await failDrill(sessionId, "accuracy");
      if (!isPostRootMoveStillCurrent(sessionId, result)) {
        return;
      }
      const store = useGameStore.getState();
      store.setDrillState("failed");
      store.setDrillTerminalReason("accuracy");
      store.setTerminalOpeningDelta(
        sessionId,
        contract.opening_score_changes ?? null,
      );
      // Reconcile the warm delta to the provably-fresh value once the background
      // recompute lands (g-fix-end-latency).
      void pollFreshOpeningDelta(sessionId, "drill_accuracy_fail");
      drillFailedMoveIndexRef.current = result.moveIndex;
      setDrillFailInfo({
        playedMoveUci: result.moveUci,
        // Trusted position best move (or honest worker best move) — never the
        // played move masquerading as best; null yields no suggestion.
        suggestionUcis: bestMove ? [bestMove] : [],
        correctionFen: result.fenBefore,
        moveIndex: result.moveIndex,
      });
      setEngineMessage("That move exceeds the allowed centipawn loss.");
      setViewIndex(result.moveIndex - 1);
      setDrillRecovery(null);
    },
    [
      isPostRootMoveStillCurrent,
      uploadFullMoveHistoryBeforeEnd,
      setEngineMessage,
      setViewIndex,
    ],
  );

  const continueAfterPlayerMove = useCallback(
    async (result: Extract<PlayerMoveApplyResult, { applied: true }>) => {
      const captured = useGameStore.getState();
      const capturedSessionId = captured.sessionId;
      const capturedDrillState = captured.drillState;

      if (!capturedSessionId) {
        if (result.gameOver) {
          await handleGameEnd();
        } else if (!isRevertPendingRef.current) {
          await applyOpponentMove(result.fenAfter, result.uciHistory);
        }
        return;
      }

      if (captured.drillOpeningKey && capturedDrillState === "root_reached") {
        const threshold = resolveStrictnessCp(
          useGameStore.getState().drillStrictnessCp,
          useGameStore.getState().drillStrictness,
        );
        // Drill-truth-first grade (g-position-analysis Phase 6): strictness-0
        // exact-best reads trusted position truth, and threshold grading reads
        // the backend-derived loss — both WITHOUT waiting on the worker when the
        // cache is sufficient. Falls back to the worker (waitForAnalysis) only
        // when no cache loss exists. Rejects only when nothing is scheduled and
        // no settled data exists (-> same recovery path as before).
        let drill: DrillGrade;
        try {
          drill = await coordinator.waitForDrillGrade(
            result.moveIndex,
            result.moveUci,
            threshold,
          );
        } catch (error) {
          if (!isPostRootMoveStillCurrent(capturedSessionId, result)) {
            return;
          }
          setDrillRecovery({ kind: "analysis", result });
          const message =
            error instanceof Error ? error.message : "Move analysis is unavailable.";
          setEngineMessage(`${message}. Try again or abandon the drill.`);
          return;
        }

        if (!isPostRootMoveStillCurrent(capturedSessionId, result)) {
          return;
        }

        // Tri-state grade: a missing/non-finite eval is `unavailable` (NOT a
        // pass) and routes to the same recovery path as a failed analysis fetch
        // so the drill never silently advances on ungraded moves. At 0cp the
        // drill requires the exact best move; above 0cp, the eval-loss boundary
        // passes (failsDrill uses strict `>`).
        if (drill.grade === "unavailable") {
          setDrillRecovery({ kind: "analysis", result });
          setEngineMessage(
            "Move analysis is unavailable. Try again or abandon the drill.",
          );
          return;
        }
        if (drill.grade === "fail") {
          try {
            await stopPostRootDrillForAccuracy(capturedSessionId, result, drill.bestMove);
          } catch (error) {
            if (!isPostRootMoveStillCurrent(capturedSessionId, result)) {
              return;
            }
            setDrillRecovery({ kind: "analysis", result });
            const message =
              error instanceof Error ? error.message : "Failed to record drill failure.";
            setEngineMessage(`${message}. Try again or abandon the drill.`);
          }
          return;
        }
      } else if (captured.drillOpeningKey && capturedDrillState === "active") {
        const canContinue = await checkPostPlayerDrillRoute(result);
        if (!canContinue) {
          return;
        }
      }

      if (result.gameOver) {
        await handleGameEnd();
      } else if (!isRevertPendingRef.current) {
        setDrillRecovery(null);
        await applyOpponentMove(result.fenAfter, result.uciHistory);
      }
    },
    [
      applyOpponentMove,
      checkPostPlayerDrillRoute,
      coordinator,
      handleGameEnd,
      isPostRootMoveStillCurrent,
      stopPostRootDrillForAccuracy,
      setEngineMessage,
    ],
  );

  // Remount resume. Both pending confirmations survive navigation, but the requests
  // that issued them do not — their promises belonged to the unmounted component,
  // and so did the recovery message and the Retry button. Re-issue once per mount so
  // a returning player finds the drill moving again instead of a board frozen behind
  // a barrier, or a pre-root drill stranded with the opponent to move and nothing
  // left to drive it. Declared after both invariant effects above, whose synchronous
  // store writes have already dropped records the current board no longer matches.
  //
  // Only the pre-root route-check is resumed here: post-root, the pending work is a
  // local analysis grade with its own recovery, and re-running it on every mount
  // would raise a banner on an ordinary route round trip.
  const didResumeDrillWorkRef = useRef(false);
  useEffect(() => {
    if (didResumeDrillWorkRef.current) return;
    didResumeDrillWorkRef.current = true;
    const store = useGameStore.getState();
    if (store.drillRootConfirm) {
      void confirmDrillRoot(store.drillRootConfirm);
      return;
    }
    if (store.drillPendingRouteMove && store.drillState === "active") {
      void continueAfterPlayerMove({
        applied: true,
        ...store.drillPendingRouteMove,
      });
    }
  }, [confirmDrillRoot, continueAfterPlayerMove]);

  const applyPlayerMoveAndAdvance = useCallback(
    (sourceSquare: string, targetSquare: string, promotion?: string): boolean => {
      const result = applyPlayerMove(sourceSquare, targetSquare, promotion);
      if (!result.applied) {
        if (result.requiresPromotion) {
          setPendingPromotion({ from: sourceSquare, to: targetSquare });
          return true; // consume the click
        }
        return false;
      }

      if (!isRevertPending) {
        void continueAfterPlayerMove(result);
      }

      return true;
    },
    [applyPlayerMove, continueAfterPlayerMove, isRevertPending],
  );

  // Clear move messages when a new game starts
  useEffect(() => {
    if (moveHistory.length === 0) {
      moveMessagesRef.current = new Map();
      setMoveMessagesVersion((v) => v + 1);
      setDrillFailInfo(null);
    }
  }, [moveHistory.length]);

  useEffect(() => {
    if (!isGameActive) {
      previousOpponentModeRef.current = null;
      setShowRehookToast(false);
      return;
    }

    const previousMode = previousOpponentModeRef.current;
    if (previousMode === "engine" && opponentMode === "ghost") {
      setShowRehookToast(true);
    }
    previousOpponentModeRef.current = opponentMode;
  }, [isGameActive, opponentMode]);

  const handleSquareClick = useCallback(
    ({ square }: { square: string }) => {
      if (pendingPromotion) {
        return; // picker is open, ignore board clicks (backdrop handles cancel)
      }

      if (isRevertPending) {
        clearMoveHighlights();
        return;
      }

      // Reviewing a past move: pieces can't be moved. Instead of a silent no-op
      // that leaves users confused, shake the board to point them at the
      // return-to-live pill (g-1y68 A3, click path; the drag path lives in
      // handleDropPiece). This runs BEFORE the blunder/drill-fail guard on
      // purpose: the rewind those trigger IS the core "blunder is shown, why
      // can't I move?" moment g-1y68 targets. Scoped to isReviewingPast — the
      // broader waiting-for-opponent case below stays a silent no-op.
      if (isReviewingPast) {
        clearMoveHighlights();
        triggerReviewShake();
        return;
      }

      if (isBlunderBoardOverrideActive) {
        clearMoveHighlights();
        return;
      }

      const playersTurn =
        chess.turn() === (playerColor === "white" ? "w" : "b");
      if (!isGameActive || !playersTurn || !isViewingLive) {
        clearMoveHighlights();
        return;
      }

      // If a square is already selected, try to make a move to the clicked square
      if (selectedSquare) {
        const result = applyPlayerMoveAndAdvance(selectedSquare, square);
        if (result) {
          return;
        }

        // Move was illegal — fall through to try selecting the new square
      }

      // Try to select a new piece
      if (!isSquare(square)) {
        clearMoveHighlights();
        return;
      }

      const piece = chess.get(square);
      const playerSide = playerColor === "white" ? "w" : "b";
      if (piece && piece.color === playerSide) {
        setSelectedSquare(square);
        getMoveOptions(square);
      } else {
        clearMoveHighlights();
      }
    },
    [
      chess,
      isGameActive,
      isBlunderBoardOverrideActive,
      isReviewingPast,
      isRevertPending,
      isViewingLive,
      pendingPromotion,
      selectedSquare,
      playerColor,
      applyPlayerMoveAndAdvance,
      clearMoveHighlights,
      getMoveOptions,
      triggerReviewShake,
    ],
  );

  useEffect(() => {
    if (!isGameActive) {
      // New/ended game resets the opening guard so the next game can fire.
      openingOpponentMoveInFlightRef.current = false;
      return;
    }

    if (isRevertPending) {
      return;
    }

    if (playerColor !== "black") {
      return;
    }

    if (moveCount > 0 || chess.turn() !== "w") {
      // Past the opening: clear the guard so a later reset/new game can fire.
      openingOpponentMoveInFlightRef.current = false;
      return;
    }

    if (engineStatus !== "ready" || isThinking || !isViewingLive) {
      return;
    }

    if (openingOpponentMoveInFlightRef.current) {
      return;
    }
    openingOpponentMoveInFlightRef.current = true;

    void applyOpponentMove(
      chess.fen(),
      useGameStore.getState().moveHistory.map((m) => m.uci),
    );
  }, [
    applyOpponentMove,
    chess,
    engineStatus,
    isGameActive,
    isRevertPending,
    isThinking,
    isViewingLive,
    moveCount,
    playerColor,
  ]);

  // Auto-dismiss flash after animation
  useEffect(() => {
    if (!showFlash) return;
    const timer = setTimeout(() => setShowFlash(false), 400);
    return () => clearTimeout(timer);
  }, [showFlash]);

  useEffect(() => {
    if (!blunderAlert) {
      clearBlunderBoardOverride();
      return;
    }

    if (!blunderAlert.shouldRewind) {
      clearBlunderBoardOverride();
      return;
    }

    clearMoveHighlights();
    for (const timer of blunderBoardTimerRefs.current) {
      clearTimeout(timer);
    }
    blunderBoardTimerRefs.current = [];

    setBoardInstanceKey((current) => current + 1);
    const startIndex = displayedIndexRef.current;
    const targetDisplayIndex = blunderAlert.moveIndex - 1;
    setViewIndex(startIndex);

    if (startIndex <= targetDisplayIndex) {
      const timer = setTimeout(() => {
        setViewIndex(targetDisplayIndex);
      }, 125);
      blunderBoardTimerRefs.current.push(timer);
      return () => {
        clearBlunderBoardOverride();
      };
    }

    for (let index = startIndex - 1, step = 0; index >= targetDisplayIndex; index -= 1, step += 1) {
      const timer = setTimeout(() => {
        setViewIndex(index);
      }, 125 + step * 240);
      blunderBoardTimerRefs.current.push(timer);
    }

    return () => {
      clearBlunderBoardOverride();
    };
  }, [
    blunderAlert,
    clearBlunderBoardOverride,
    clearMoveHighlights,
    setViewIndex,
  ]);

  // Auto-dismiss re-hook toast after 3 seconds
  useEffect(() => {
    if (!showRehookToast) return;
    const timer = setTimeout(() => setShowRehookToast(false), 3000);
    return () => clearTimeout(timer);
  }, [showRehookToast]);

  useEffect(() => {
    if (!copyPositionNotice) return;
    const noticeNonce = copyPositionNotice.nonce;
    const timer = window.setTimeout(() => {
      setCopyPositionNotice((current) =>
        current?.nonce === noticeNonce ? null : current,
      );
    }, 1800);
    return () => window.clearTimeout(timer);
  }, [copyPositionNotice]);

  useEffect(() => {
    if (!streakToast) return;
    const timer = setTimeout(() => setStreakToast(null), 2400);
    return () => clearTimeout(timer);
  }, [streakToast]);

  useEffect(() => {
    if (blocksStreakToast) {
      setStreakToast(null);
    }
  }, [blocksStreakToast]);

  // Close ghost info popover on click outside
  useEffect(() => {
    if (!showGhostInfo) return;
    const handler = (e: MouseEvent) => {
      if (ghostInfoAnchorRef.current && !ghostInfoAnchorRef.current.contains(e.target as Node)) {
        setShowGhostInfo(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showGhostInfo]);

  const handleDropPiece = useCallback(
    ({
      sourceSquare,
      targetSquare,
    }: PieceDropHandlerArgs) => {
      if (isRevertPending || !targetSquare) {
        return false;
      }

      // Reviewing a past move (manual nav OR a blunder/drill-fail rewind):
      // dragging is enabled only so a drag ATTEMPT lands here (players make moves
      // by dragging, not just clicking). Reject the drop and shake to point at
      // the return-to-live pill (g-1y68 A3, drag path; the click path lives in
      // handleSquareClick). Runs before the blunder/drill-fail guard so the core
      // "blunder is shown" rewind gets the cue too.
      if (isReviewingPast) {
        triggerReviewShake();
        return false;
      }

      if (isBlunderBoardOverrideActive) {
        return false;
      }

      const result = handleDrop(sourceSquare, targetSquare);
      if (!result.applied) {
        if (result.requiresPromotion) {
          setPendingPromotion({ from: sourceSquare, to: targetSquare });
        }
        return false; // piece snaps back in both cases
      }

      if (!isRevertPending) {
        void continueAfterPlayerMove(result);
      }

      return true;
    },
    [
      continueAfterPlayerMove,
      handleDrop,
      isBlunderBoardOverrideActive,
      isReviewingPast,
      isRevertPending,
      triggerReviewShake,
    ],
  );

  const handlePromotionPick = useCallback(
    (piece: 'q' | 'r' | 'b' | 'n') => {
      if (!pendingPromotion) return;
      const store = useGameStore.getState();
      const isLive = store.viewIndex === null;
      const isCorrectTurn = chess.turn() === (store.playerColor === 'white' ? 'w' : 'b');
      if (!store.isGameActive || isRevertPending || !isLive || !isCorrectTurn) {
        setPendingPromotion(null);
        return;
      }
      const { from, to } = pendingPromotion;
      setPendingPromotion(null);
      applyPlayerMoveAndAdvance(from, to, piece);
    },
    [pendingPromotion, chess, applyPlayerMoveAndAdvance, isRevertPending],
  );

  const handlePromotionCancel = useCallback(() => {
    setPendingPromotion(null);
    clearMoveHighlights();
  }, [clearMoveHighlights]);

  const handleRevealSrsFail = useCallback(
    (detail: SrsFailDetail, moveIndex: number) => {
      if (isRevertPendingRef.current) {
        return;
      }
      clearBlunderBoardOverride();
      setBlunderAlert(null);
      setReviewFailModal({
        userMoveSan: detail.userMoveSan,
        bestMoveSan: detail.bestMoveSan,
        userMoveUci: detail.userMoveUci,
        bestMoveUci: detail.bestMoveUci,
        evalLoss: 0,
        moveIndex,
      });
      setViewIndex(moveIndex - 1);
    },
    [clearBlunderBoardOverride, setViewIndex],
  );

  // SRS fail spotlight: auto-reveal the blunder arrows + bubble (same path as a
  // manual reveal) and bump the nonce trigger so the full-screen spotlight
  // (re)starts cleanly even for back-to-back fails.
  const triggerSrsFailSpotlight = useCallback(
    (detail: SrsFailDetail, moveIndex: number) => {
      handleRevealSrsFail(detail, moveIndex);
      srsFailNonceRef.current += 1;
      setSrsFailTrigger({ id: srsFailNonceRef.current, moveIndex });
    },
    [handleRevealSrsFail],
  );

  const handleSrsFailDone = useCallback((id: number) => {
    setSrsFailTrigger((prev) => (prev?.id === id ? null : prev));
  }, []);

  const flipBoard = () => {
    setBoardOrientation((current) => (current === "white" ? "black" : "white"));
  };

  const gameStatusBadge = deriveGameStatusBadge(isGameActive, gameResult);
  const squareStyles = useMemo(
    () => ({ ...lastMoveSquares, ...optionSquares }),
    [lastMoveSquares, optionSquares],
  );

  const handleCloseStartOverlay = useCallback(() => {
    setShowStartOverlay(false);
    // Restore the natural-end banner if cancelling the gear/settings overlay.
    // Successful starts close the overlay via handleNewDrill, not this handler,
    // so the banner won't wrongly reappear after a started drill.
    if (useGameStore.getState().gameResult) {
      setShowPostGamePrompt(true);
    }
  }, []);

  const handleStartDrill = useCallback(async (draft: StartDrillDraft) => {
    // The draft carries the committed values from the start panel; the panel
    // guards against a null opening (Start is disabled until one is picked).
    const result = await handleNewDrill({
      openingKey: draft.opening.opening_key,
      playerColor: draft.playerColor,
      engineElo: draft.engineElo,
      strictness: strictnessFromCp(draft.strictnessCp),
      strictnessCp: draft.strictnessCp,
      // null for a registered root → backend drills it via the book BFS; a line
      // (incl. an off-book card's exact played line) drives the strict route.
      line: draft.line ?? undefined,
    });

    if (result) {
      // Sync the seed scratch to the committed draft so a later overlay open
      // can't resurrect a stale ad-hoc opening/line after the panel locally
      // switched openings (g-fxrm). A null draft line clears the ad-hoc ref.
      setSelectedDrillOpening(draft.opening);
      adHocLineRef.current = draft.line;
      // Only discard the prior stopped drill's failed index once the
      // replacement drill is actually live — a failed start returns to the
      // old stopped drill, which still needs its targeted barrier/index.
      drillFailedMoveIndexRef.current = null;
      setAnalyzeError(null);
      if (chess.turn() !== (draft.playerColor === "white" ? "w" : "b")) {
        void applyOpponentMove(result.fen, result.uciHistory);
      }
    }
  }, [handleNewDrill, chess, applyOpponentMove]);

  // Open the drill setup overlay for changing settings (gear button / fallback).
  // Does NOT clear gameResult, so the natural-end banner is preserved and can be
  // restored if the overlay is cancelled (see handleCloseStartOverlay).
  const handleAgainSettings = useCallback(() => {
    // Leaving the reviewed-return presentation for the setup overlay: clear it
    // up front, then seed the overlay from the retained exact store settings.
    setIsReviewedDrillReturn(false);
    const s = useGameStore.getState();
    if (s.drillOpeningKey != null) {
      // Seed the setup panel from the live (exact) store state. Guard the
      // localStorage prefill effect so it doesn't clobber these values.
      skipStickyPrefillRef.current = true;
      // Strictness is NOT reseeded from the store (g-09mu force-always): the
      // reopened panel starts with no tier selected, forcing a fresh pick.
      setDrillStrictnessCp(null);
      // Re-randomize opponent difficulty (g-ncvm) across the full bin ladder
      // (g-acsr); the user can still adjust the slider before Start. Seeds the
      // panel draft only — the store commits on Start, not on open (g-fxrm).
      setSeedEngineElo(sampleDrillEloBin());
      setDrillPlayerColor(s.playerColor === "black" ? "black" : "white");
      if (s.drillLine != null) {
        // Ad-hoc drill: restore the synthetic selection + line from the durable
        // store. The roots list can't resolve a non-root target FEN, so seeding
        // pendingDrillSetupRef would leave the overlay with no selection.
        setSelectedDrillOpening({
          opening_key: s.drillOpeningKey,
          opening_name: s.drillOpeningName ?? "Custom line",
          opening_family: "",
          eco: null,
          depth: s.drillLine.length,
        });
        adHocLineRef.current = s.drillLine;
        pendingDrillSetupRef.current = null;
      } else {
        adHocLineRef.current = null;
        pendingDrillSetupRef.current = {
          openingKey: s.drillOpeningKey,
          playerColor: s.playerColor,
        };
      }
    }
    // When no drill state exists, fall back to the localStorage-prefill effects.

    setIsDrillMode(true);
    setShowPostGamePrompt(false);
    setShowStartOverlay(true);
  }, []);

  // Instantly restart the drill: opening/side/strictness replay exactly, but
  // opponent difficulty is re-randomized uniformly over every bin (g-ncvm,
  // widened by g-acsr).
  const handleAgainDrill = useCallback(async (
    surface: "drill_stop" | "post_game",
    event?: ReactMouseEvent<HTMLButtonElement>,
  ) => {
    if (isStartingGame) return;
    const s = useGameStore.getState();
    const matchingDelta =
      s.sessionId != null && s.openingScoreDelta?.sessionId === s.sessionId
        ? s.openingScoreDelta
        : null;

    // The live store read is authoritative: rendering can race a fresh commit or
    // an unavailable transition. No restart side effect may precede this guard.
    if (matchingDelta?.freshness === "pending") {
      const snapshot = getOpeningDeltaPollSnapshot(s.sessionId!);
      captureEvent("drill_again_blocked", {
        surface,
        trigger: snapshot?.trigger ?? "unknown",
        wait_elapsed_ms: snapshot?.elapsedMs ?? null,
        input_method: classifyDrillAgainInput(event?.nativeEvent),
        visibility: getOpeningDeltaVisibility(),
      });
      return;
    }

    const openingDeltaStateAtClick =
      matchingDelta?.freshness === "fresh"
        ? "fresh"
        : matchingDelta?.freshness === "unavailable"
          ? "unavailable"
          : "not_applicable";
    // Exact replay is impossible without the exact cp — open the overlay
    // instead (it samples its own Elo). Sample AFTER this guard so the value
    // logged in telemetry is exactly the value actually used.
    if (!s.drillOpeningKey || s.drillStrictness == null || s.drillStrictnessCp == null) {
      captureEvent("drill_again_clicked", {
        opening_key: s.drillOpeningKey ?? null,
        player_color: s.playerColor,
        engine_elo: null, // no Elo committed here; the overlay will sample
        surface,
        opening_delta_state_at_click: openingDeltaStateAtClick,
      });
      handleAgainSettings();
      return;
    }

    // Re-randomize opponent difficulty (g-ncvm) uniformly across the whole bin
    // ladder rather than around the player's rating (g-acsr), so repeated Agains
    // draw out a wider spread of replies; opening/side/strictness stay fixed.
    const nextEngineElo = sampleDrillEloBin();
    captureEvent("drill_again_clicked", {
      opening_key: s.drillOpeningKey,
      player_color: s.playerColor,
      engine_elo: nextEngineElo, // logged value == value actually used
      surface,
      opening_delta_state_at_click: openingDeltaStateAtClick,
    });

    const result = await handleNewDrill({
      openingKey: s.drillOpeningKey,
      playerColor: s.playerColor,
      engineElo: nextEngineElo,
      strictness: s.drillStrictness,
      strictnessCp: s.drillStrictnessCp,
      // Replaying an ad-hoc drill needs its line: the store key is a target FEN,
      // not a registered root, so without the line the backend would 404. Read
      // it from the DURABLE store (not adHocLineRef) so the reviewed-return path
      // works after the /drill-analysis route remounts this component.
      line: s.drillLine ?? undefined,
    });

    if (result) {
      drillFailedMoveIndexRef.current = null;
      setAnalyzeError(null);
      // handleNewDrill already cleared isReviewedDrillReturn in its success path.
      // Opponent-first restarts (player is black) are driven by the
      // opponent-move effect, which runs after the new session is committed to
      // the store. Calling applyOpponentMove directly here would capture the
      // pre-restart sessionId and fire a request against the abandoned session.
    } else {
      // handleNewDrill clears end state only after a successful start, so the
      // natural-end banner is intact — open the overlay preserving it.
      handleAgainSettings();
    }
  }, [isStartingGame, handleNewDrill, handleAgainSettings]);

  const handlePostGameDrillAgain = useCallback(
    (event: ReactMouseEvent<HTMLButtonElement>) => {
      void handleAgainDrill("post_game", event);
    },
    [handleAgainDrill],
  );
  const handleStoppedDrillAgain = useCallback(
    (event: ReactMouseEvent<HTMLButtonElement>) => {
      void handleAgainDrill("drill_stop", event);
    },
    [handleAgainDrill],
  );

  // Reviewed-return "View analysis": the saved snapshot is still in the store
  // (it was never cleared on return), so just re-open the review. Never rebuild
  // via handleAnalyzeDrill here — the live analysis map was cleared on the way
  // out and a rebuild would overwrite the good snapshot with an empty one.
  const handleViewDrillReview = useCallback(() => {
    if (useDrillAnalysisStore.getState().snapshot) {
      navigate("/drill-analysis");
    }
  }, [navigate]);

  const handleSwitchToDrillMode = useCallback(() => setIsDrillMode(true), []);
  const handleSwitchToPlayMode = useCallback(() => setIsDrillMode(false), []);

  const handleStartPlay = useCallback(
    (side: "white" | "random" | "black", elo: number) => {
      // Commit the drafted difficulty to the store, then start: handleNewGame
      // reads engineElo via getState(), and Zustand's set is synchronous.
      setEngineElo(elo as (typeof MAIA_ELO_BINS)[number]);
      void handleNewGame(side);
    },
    [handleNewGame, setEngineElo],
  );
  const handleToggleGhostInfo = useCallback(
    () => setShowGhostInfo((v) => !v),
    [],
  );
  const handleCloseGhostInfo = useCallback(
    () => setShowGhostInfo(false),
    [],
  );
  const canRetryDrillSteering =
    Boolean(engineMessage) &&
    drillOpeningKey !== null &&
    isGameActive &&
    !isRevertPending &&
    isViewingLive &&
    (drillRecovery?.kind === "root-confirm"
      ? // A failed root confirmation. The opponent already moved into the root, so
        // it IS the player's turn — the !isPlayersTurn gate below cannot apply.
        drillState === "active"
      : // Everything else must never fall through to applyOpponentMove while a
        // confirmation is outstanding: that would advance the drill past a root it
        // has not been proven to have reached.
        rootConfirm === null &&
        !isPlayersTurn &&
        (drillState === "active" ||
          (drillState === "root_reached" && drillRecovery !== null)));
  const handleRetryDrillSteering = useCallback(() => {
    if (!canRetryDrillSteering) return;
    setEngineMessage(null);
    if (drillRecovery?.kind === "analysis") {
      const result = drillRecovery.result;
      coordinator.restartAnalysisWorker();
      coordinator.analyzeMove(result.fenBefore, result.moveUci, playerColor, result.moveIndex);
      void continueAfterPlayerMove(result);
      return;
    }
    if (drillRecovery?.kind === "root-confirm") {
      void confirmDrillRoot(drillRecovery.request);
      return;
    }
    if (drillRecovery?.kind === "player-route") {
      // Re-entry is safe and complete: the move is already applied to `chess` and
      // drillState is still "active", so continueAfterPlayerMove re-runs the
      // route-check and, on success, carries on to handleGameEnd or the opponent
      // request exactly as the first pass would have. The move comes from the
      // durable record, and checkPostPlayerDrillRoute re-validates its identity
      // before dispatching.
      const pending = useGameStore.getState().drillPendingRouteMove;
      if (pending) {
        void continueAfterPlayerMove({ applied: true, ...pending });
      }
      return;
    }
    if (drillRecovery?.kind === "opponent") {
      void applyOpponentMove(drillRecovery.fen, drillRecovery.uciHistory);
      return;
    }
    void applyOpponentMove(
      chess.fen(),
      useGameStore.getState().moveHistory.map((m) => m.uci),
    );
  }, [
    applyOpponentMove,
    canRetryDrillSteering,
    chess,
    confirmDrillRoot,
    continueAfterPlayerMove,
    coordinator,
    drillRecovery,
    playerColor,
    setEngineMessage,
  ]);

  const canDragLiveMove =
    isGameActive &&
    isPlayersTurn &&
    !isRevertPending &&
    isViewingLive &&
    !isBlunderBoardOverrideActive &&
    rootConfirm === null;
  // Also let the piece lift while reviewing a past move — not to allow a move
  // (handleDropPiece rejects it) but so a drag ATTEMPT fires onPieceDrop and can
  // trigger the return-to-live shake (g-1y68 A3 drag path). Includes the
  // blunder/drill-fail rewind (the core g-1y68 case); only the revert dialog,
  // a true modal, stays non-interactive.
  const allowDragging =
    canDragLiveMove || (isReviewingPast && !isRevertPending);
  const showEndedScrim = !isGameActive && gameResult !== null && !showStartOverlay;
  const hasBelowBoardContent = moveHistory.length > 0 || !isGameActive;

  // Drop the fanfare nonce whenever the terminal display state ends (g-8079).
  // Required for correctness on top of the render gate below: unmounting the
  // child (gate → null) runs its timer cleanup, so onDone may never fire and the
  // nonce would linger forever; and reopening→cancelling the post-game start
  // overlay (showEndedScrim false→true) would otherwise replay the stale nonce.
  // Clearing on the false transition covers new game (isGameActive→true), reset
  // (gameResult→null), and start-overlay open in one place, and never clobbers
  // the just-set trigger at game end (that render has showEndedScrim === true).
  useEffect(() => {
    if (!showEndedScrim) setEndGameFanfare(null);
  }, [showEndedScrim]);

  // Mobile portrait: when below-board content (the analysis graph) first
  // appears, scroll the nav/hamburger header out of view so the graph lands in
  // the viewport. Only on a false→true transition while narrow; seed on first
  // run so an initial mount with existing moves does not jump.
  const isNarrow = useMediaQuery(GAME_MOBILE_QUERY);
  const sectionRef = useRef<HTMLElement | null>(null);
  const prevHasBelowRef = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    const prev = prevHasBelowRef.current;
    prevHasBelowRef.current = hasBelowBoardContent;
    if (prev === undefined) return; // seed only — no scroll on first run
    if (!isNarrow) return;
    if (prev || !hasBelowBoardContent) return; // only false→true transitions
    const behavior: ScrollBehavior = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches
      ? "auto"
      : "smooth";
    sectionRef.current?.scrollIntoView({ block: "start", behavior });
  }, [hasBelowBoardContent, isNarrow]);

  // Opponent captures (panel) use the opposite perspective; player captures
  // (controls row) use the player's. Only supplied in portrait via CSS, but the
  // values are always threaded — the targets render conditionally.
  const panelMaterialFen = isNarrow ? displayedFen : undefined;
  const controlsMaterialFen = isNarrow ? displayedFen : undefined;

  return (
    <AnalysisStoreProvider value={analysisStore}>
      <section className="chess-section" ref={sectionRef}>
        <div className={`chess-layout ${hasBelowBoardContent ? 'has-graph' : ''}`}>
          <GameInfoPanel
            statusText={statusText}
            gameStatusBadge={gameStatusBadge}
            isRated={isRated}
            isPracticeContinuation={isPracticeContinuation}
            isStoppedDrill={isStoppedDrill}
            isGameActive={isGameActive}
            isActiveDrill={isActiveDrill}
            drillOpeningName={drillOpeningName}
            playerColorChoice={playerColorChoice}
            playerColor={playerColor}
            playerRating={playerRating}
            isProvisional={isProvisional}
            ratingScores={ratingScores}
            opponentMode={opponentMode}
            opponentName={MAIA_BOT_NAMES[engineElo as keyof typeof MAIA_BOT_NAMES]}
            engineElo={engineElo}
            gameResult={gameResult}
            blunderReviewId={blunderReviewId}
            showGhostInfo={showGhostInfo}
            onToggleGhostInfo={handleToggleGhostInfo}
            onCloseGhostInfo={handleCloseGhostInfo}
            ghostInfoAnchorRef={ghostInfoAnchorRef}
            blunderTargetFen={blunderTargetFen}
            boardOrientation={boardOrientation}
            blunderReviewSrs={blunderReviewSrs}
            openingLineageSlot={
              (isGameActive || gameResult !== null) &&
              openingLineage.length > 0 ? (
                <div className="chess-panel__openings">
                  <GameOpeningLineage
                    playerColor={openingLineagePlayerColor}
                    lineage={openingLineage}
                    startPly={openingLineageStartPly}
                    scoreChanges={openingScoreChanges}
                    scoreStatus={openingScoreStatus}
                    pendingScoreIndices={openingLineagePendingScoreIndices}
                    // Keep the expanded card in sync with the board (g-m1xc).
                    // displayedIndex — not viewIndex — is the board's move
                    // cursor: it normalizes "live/latest" to the last ply and
                    // keeps -1 for the starting position.
                    activeMoveIndex={displayedIndex}
                    // Board navigation works during play AND post-game (history
                    // parity): selecting a card only REVIEWS the opening's past
                    // position (viewIndex) — it never disturbs the live game.
                    // Rated live games keep the Elo-integrity guard. Active,
                    // root-reached, and stopped drills can be replaced safely.
                    onSelectRoot={handleLineageSelectRoot}
                    onStartDrill={
                      gameResult !== null || canStartDrillWhileGameActive
                        ? handleLineageStartDrill
                        : undefined
                    }
                  />
                </div>
              ) : null
            }
            perfectStreak={perfectStreak}
            materialFen={panelMaterialFen}
            materialPerspective={opponentColor}
          />

          <div className="chessboard-wrapper">
            <div className="chessboard-board-with-eval">
              <ConnectedEvalBar />
              <BoardStage
                boardInstanceKey={boardInstanceKey}
                boardOrientation={boardOrientation}
                displayedFen={displayedFen}
                onPieceDrop={handleDropPiece}
                onSquareClick={handleSquareClick}
                allowDragging={allowDragging}
                squareStyles={squareStyles}
                arrows={blunderArrows}
                showStartOverlay={showStartOverlay}
                isGameActive={isGameActive}
                canStartDrillWhileGameActive={canStartDrillWhileGameActive}
                isReviewingPast={isReviewingPast}
                onReturnToLive={handleReturnToLive}
                reviewNudge={reviewNudge}
                isStoppedDrill={isStoppedDrill}
                isStartingGame={isStartingGame}
                onCloseStartOverlay={handleCloseStartOverlay}
                maiaEloBins={MAIA_ELO_BINS}
                seedEngineElo={seedEngineElo}
                seedStrictnessCp={drillStrictnessCp}
                seedColor={drillPlayerColor}
                seedOpening={selectedDrillOpening}
                seedLine={adHocLineRef.current}
                playerRating={playerRating}
                isProvisional={isProvisional}
                onStartPlay={handleStartPlay}
                startError={startError}
                showRevertWarning={showRevertWarning}
                isRevertPending={isRevertPending}
                revertError={revertError}
                onRevertAnyway={executeRevert}
                onCancelRevert={cancelRevert}
                showResignWarning={showResignWarning}
                isPracticeContinuation={isPracticeContinuation}
                onResignAnyway={executeResign}
                onCancelResign={cancelResign}
                showEndedScrim={showEndedScrim}
                showFlash={showFlash}
                pendingPromotion={pendingPromotion}
                playerColor={playerColor}
                onPromotionPick={handlePromotionPick}
                onPromotionCancel={handlePromotionCancel}
                streakToast={blocksStreakToast ? null : streakToast}
                lastDrillDeltaToast={lastDrillDeltaToast}
                onDismissLastDrillDelta={dismissLastDrillDelta}
                boardNotice={boardNotice}
                copyPositionNotice={copyPositionNotice}
                isDrillMode={isDrillMode}
                onSwitchToPlayMode={handleSwitchToPlayMode}
                onSwitchToDrillMode={handleSwitchToDrillMode}
                openingFamilies={openingFamilies}
                onStartDrill={handleStartDrill}
                isLoadingOpenings={isLoadingOpenings}
                srsFailTrigger={srsFailTrigger}
                onSrsFailDone={handleSrsFailDone}
                endGameFanfareTrigger={
                  showEndedScrim && !pendingPromotion ? endGameFanfare : null
                }
                onEndGameFanfareDone={handleEndGameFanfareDone}
              />
            </div>
          </div>
          {hasBelowBoardContent && (
            <div className="chess-graph-area">
              <PostGameBanner
                isGameActive={isGameActive}
                isPracticeContinuation={isPracticeContinuation}
                showPostGamePrompt={showPostGamePrompt}
                gameResult={gameResult}
                drillOpeningKey={drillOpeningKey}
                drillState={drillState}
                isReviewedDrillReturn={isReviewedDrillReturn}
                onNewDrill={handlePostGameDrillAgain}
                onAnotherDrillSettings={handleAgainSettings}
                drillActionsDisabled={isStartingGame}
                drillAgainPending={isDrillDeltaPending}
                ratingChange={ratingChange}
                scoreChanges={scoreChanges}
                onViewAnalysis={handleViewAnalysis}
                onShowStartOverlay={handleShowStartOverlay}
              />
              <ConnectedAnalysisGraph onSelectMove={handleNavigate} />
            </div>
          )}

          <div className="moves-column">
            <MaterialDisplay fen={displayedFen} perspective={opponentColor} />
            {((isGameActive && isStoppedDrill && !gameResult) ||
              isReviewedDrillReturn) && (
              <DrillStopActions
                terminalReason={drillTerminalReason}
                onAnotherDrill={handleStoppedDrillAgain}
                onAnotherDrillSettings={handleAgainSettings}
                drillAgainPending={isDrillDeltaPending}
                // Live stop rebuilds + opens the review; reviewed return just
                // re-opens the saved snapshot (rebuilding would wipe it, since
                // the live analysis map was cleared on the way out).
                onAnalyze={
                  isReviewedDrillReturn ? handleViewDrillReview : handleAnalyzeDrill
                }
                analyzeEnabled={moveHistory.length > 0}
                isPreparing={isPreparingAnalysis}
                disabled={isStartingGame}
                errorMessage={analyzeError}
              />
            )}
            {isGameActive && drillOpeningKey && engineMessage && !isStoppedDrill && (
              <div className="chess-start-error" role="alert">
                <p>{engineMessage}</p>
                <div className="chess-post-game-actions">
                  {canRetryDrillSteering && (
                    <button
                      className="chess-button primary"
                      type="button"
                      onClick={handleRetryDrillSteering}
                    >
                      Retry
                    </button>
                  )}
                  {drillState !== "converted" && (
                    <button
                      className="chess-button"
                      type="button"
                      onClick={handleResignClick}
                    >
                      Abandon
                    </button>
                  )}
                </div>
              </div>
            )}
            {isGameActive && !isPlayersTurn && (
              <span className="turn-label">
                Waiting for opponent
                <span className="turn-label-spinner" aria-hidden="true" />
              </span>
            )}
            <ConnectedMoveList
              onNavigate={handleNavigate}
              messages={moveMessages}
              onRevealSrsFail={handleRevealSrsFail}
              revealedSrsFailIndex={reviewFailModal?.moveIndex ?? null}
              onResign={handleResignClick}
              isResignDisabled={!isGameActive || chess.isGameOver()}
              onRevert={handleRevertClick}
              isRevertDisabled={moveHistory.length === 0 || chess.isGameOver()}
              onFlipBoard={flipBoard}
              onCopyPosition={handleCopyPosition}
              onReset={handleReset}
              isGameActive={isGameActive}
              isInteractionDisabled={isRevertPending}
              materialFen={controlsMaterialFen}
            />
            {isGameActive && isPlayersTurn && (
              <span className="turn-label">Your turn</span>
            )}
            <MaterialDisplay fen={displayedFen} perspective={playerColor} />
          </div>
        </div>

        <AnalysisEffects
          appendMoveMessage={appendMoveMessage}
          setBlunderAlert={setBlunderAlert}
          setShowFlash={setShowFlash}
          setResolvedReview={setResolvedReview}
          onSrsFail={triggerSrsFailSpotlight}
          coordinator={coordinator}
        />
      </section>
    </AnalysisStoreProvider>
  );
};

export default ChessGame;
