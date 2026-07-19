import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchSessionOpenings,
  type OpeningLineageItem,
  type OpeningPlayerColor,
  type OpeningScoreStatus,
} from "../utils/api";

interface UseSessionOpeningsOptions {
  /** Bump to force a refetch (live move count, or analysis move count). */
  refetchKey: number;
  /** When set together with `active`, schedule a bounded series of delayed
   *  re-fetches (up to LAG_REPOLL_MAX_TICKS) after each `refetchKey` change to
   *  converge the upload->display lag. Omit (or `active=false`) to disable. */
  lagRepollMs?: number;
  /** Gate the lag re-poll — only re-poll while the game is active. */
  active?: boolean;
}

interface SessionOpeningsState {
  sessionId: string | null;
  lineage: OpeningLineageItem[];
  playerColor: OpeningPlayerColor;
  startPly: number;
  scoreStatus: OpeningScoreStatus;
}

// Stable empty reference so a session change / disabled hook does not churn
// renders via a fresh array each time.
const EMPTY_LINEAGE: OpeningLineageItem[] = [];

// How many delayed re-fetches follow each refetchKey change while active. Two
// ticks cover a ply whose server-side upload/analysis lands after the local
// move event; when the position is stable there is no further traffic.
const LAG_REPOLL_MAX_TICKS = 2;

// Score reconciliation (g-a5v3): a cold score cache answers immediately with
// `score_status: "pending"` instead of blocking, so the client must come back
// for the scores.
//
// The interval MUST exceed the scheduler's 1.5s quiet_window. A 1500ms cadence
// equals the debounce window and keeps the key maximally hot; the backend's
// is-already-scheduled guard stops the deadline from being pushed out, but
// there is still no point polling faster than the work can land.
const PENDING_REPOLL_MS = 3000;
// ~8 attempts at 3s ≈ 24s. On exhaustion the hook reports the scores as
// resolved-but-absent so consumers can clear their loading affordance — the
// status alone never changes, so without this a spinner would run forever.
const PENDING_REPOLL_MAX_ATTEMPTS = 8;
// How many consecutive NON-pending (request-free) ticks the reconciliation
// cycle waits through before going quiet. Covers a first fetch that resolves
// after the first tick; see the effect for why stopping immediately is wrong.
const PENDING_REPOLL_IDLE_TICKS = 3;

/**
 * Fetch a session's opening lineage (broadest -> deepest) for live + history
 * display. Owns the session-scoped state and reset so callers never leak one
 * game's stack into another:
 *
 *  - `sessionId === null` disables fetching entirely (HistoryPage passes null
 *    while analysis has no moves, preserving its zero-move skip).
 *  - A session change yields `[]` synchronously via the derived guard — no flash
 *    of the previous game's stack — held until the new session's fetch commits.
 *  - Same-session refetches keep the prior lineage on screen until the new result
 *    resolves (no empty flash between ticks).
 *  - With `lagRepollMs` + `active`, a bounded sequential re-poll (at most
 *    LAG_REPOLL_MAX_TICKS delayed re-fetches per refetchKey change; never
 *    setInterval, never a synchronous fetch) converges the lag for plies that
 *    upload after their local move event, then goes quiet — no fixed-interval
 *    traffic while idle at a position. The re-poll effect cannot fetch — only
 *    re-arm — so toggling `active` never triggers a data fetch (finding C).
 *  - Out-of-order safety: a monotonic sequence number ensures a slow OLDER
 *    response can never overwrite a newer/deeper lineage for the same session.
 *
 * The fetch effect intentionally re-fetches on EVERY (sessionId, refetchKey)
 * change rather than diffing a "last fetched" key. That keeps it correct under
 * React StrictMode's setup/cleanup/setup in dev: the first (aborted) request
 * never commits and the second request loads the data. (A key-diff guard would
 * mark the key fetched on the aborted first pass and skip the second.)
 */
export function useSessionOpenings(
  sessionId: string | null,
  { refetchKey, lagRepollMs, active = false }: UseSessionOpeningsOptions,
): {
  lineage: OpeningLineageItem[];
  playerColor: OpeningPlayerColor;
  startPly: number;
  /** Effective status for display: "pending" only while scores are genuinely
   *  still coming. Flips to "ready" once the bounded reconciliation gives up,
   *  so a permanently-cold cache renders as unscored rather than loading. */
  scoreStatus: OpeningScoreStatus;
} {
  const [state, setState] = useState<SessionOpeningsState>({
    sessionId: null,
    lineage: EMPTY_LINEAGE,
    playerColor: "white",
    startPly: 1,
    scoreStatus: "ready",
  });
  // Which reconciliation WINDOW ran out of attempts, as `sessionId::refetchKey`.
  //
  // Keyed by the window rather than a boolean (or the session alone) so it is
  // DERIVED as cleared whenever a new budget arms — no reset effect, no
  // cascading render. Including refetchKey matters: the re-poll effect re-arms a
  // fresh attempt budget on every refetchKey change, so keying on the session
  // alone would leave a session permanently "ready" after one exhausted window,
  // and later moves in that game could never show the loading affordance again.
  const [exhaustedWindow, setExhaustedWindow] = useState<string | null>(null);
  const reconcileWindow = `${sessionId ?? ""}::${refetchKey}`;

  // Monotonic across ALL fetches (data-change + poll) so only the latest issued
  // request may commit; a slower OLDER response is dropped.
  const seqRef = useRef(0);
  // The in-flight request; each new fetch aborts the previous one.
  const controllerRef = useRef<AbortController | null>(null);
  // Guards setState after unmount (also across StrictMode's setup/cleanup/setup).
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      controllerRef.current?.abort();
    };
  }, []);

  const doFetch = useCallback((sid: string): Promise<void> => {
    seqRef.current += 1;
    const mySeq = seqRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    return fetchSessionOpenings(sid, { signal: controller.signal })
      .then((data) => {
        // Commit only if this is still the latest request and we're mounted.
        if (!mountedRef.current || mySeq !== seqRef.current) return;
        setState({
          sessionId: sid,
          lineage: data.lineage,
          playerColor: data.player_color,
          startPly: data.start_ply,
          scoreStatus: data.score_status,
        });
      })
      .catch(() => {
        if (!mountedRef.current || mySeq !== seqRef.current) return;
        // Keep prior same-session data on a transient error; only blank the
        // FIRST load of a session with nothing established yet.
        setState((prev) =>
          prev.sessionId === sid
            ? prev
            : {
                sessionId: sid,
                lineage: EMPTY_LINEAGE,
                playerColor: "white",
                startPly: 1,
                scoreStatus: "ready",
              },
        );
      });
  }, []);

  // Fetch whenever the session or refetchKey changes (and on mount). Does NOT
  // depend on `active`, so toggling active never triggers a fetch (finding C).
  useEffect(() => {
    if (sessionId == null) return;
    void doFetch(sessionId);
  }, [sessionId, refetchKey, doFetch]);

  // Bounded lag re-poll while active: each (sessionId, refetchKey, active) arm
  // schedules at most LAG_REPOLL_MAX_TICKS delayed re-fetches, then goes quiet.
  // Fires ONLY on a timer tick (never synchronously), re-arming after each
  // request settles so requests never overlap. Tearing down (active false /
  // dep change / unmount) cancels the cycle so no further ticks.
  useEffect(() => {
    if (sessionId == null || !active || !lagRepollMs) return;
    const sid = sessionId;
    let cancelled = false;
    let ticksLeft = LAG_REPOLL_MAX_TICKS;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = () => {
      if (cancelled) return;
      ticksLeft -= 1;
      void doFetch(sid).finally(() => {
        if (cancelled || ticksLeft <= 0) return;
        timer = setTimeout(tick, lagRepollMs);
      });
    };
    timer = setTimeout(tick, lagRepollMs);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [sessionId, refetchKey, active, lagRepollMs, doFetch]);

  // Score reconciliation for a cold cache (g-a5v3). Deliberately gated ONLY on
  // the pending status — NOT on `active` or `lagRepollMs`:
  //   - HistoryPage passes neither, and would otherwise never reconcile.
  //   - ChessGame passes `active: isGameActive`, which goes false at exactly
  //     the terminal moment the score badges need their numbers.
  // The re-poll for upload lag above stays as-is; these are separate concerns.
  //
  // `scoreStatus` is read through a ref and kept OUT of the dep array (finding
  // C): a status flip must not re-arm the cycle, and this effect — like the lag
  // re-poll — may only fetch on a TIMER TICK, never synchronously on a dep
  // change. The tick closes over its own counter for the same reason.
  // Synced in an effect, not during render: a render-phase ref write is unsafe
  // under concurrent rendering. The only reader is the timer tick below, which
  // runs long after commit, so the one-commit lag is not observable.
  const scoreStatusRef = useRef<OpeningScoreStatus>("ready");
  useEffect(() => {
    scoreStatusRef.current = state.scoreStatus;
  }, [state.scoreStatus]);

  useEffect(() => {
    if (sessionId == null) return;
    const sid = sessionId;
    let cancelled = false;
    let attemptsLeft = PENDING_REPOLL_MAX_ATTEMPTS;
    // A tick that finds the status NOT pending must not stop the cycle
    // outright: the very first fetch may still be in flight (it resolves after
    // this tick whenever the request outruns PENDING_REPOLL_MS), so the ref
    // would still read the initial "ready" and reconciliation would never
    // start. Idle ticks are cheap — they issue no request — but they are
    // bounded so a session whose scores are simply ready goes quiet.
    let idleTicksLeft = PENDING_REPOLL_IDLE_TICKS;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = () => {
      if (cancelled) return;
      if (scoreStatusRef.current !== "pending") {
        if (idleTicksLeft <= 0) return;
        idleTicksLeft -= 1;
        timer = setTimeout(tick, PENDING_REPOLL_MS);
        return;
      }
      if (attemptsLeft <= 0) {
        // Bounded give-up: surface it so consumers stop showing a spinner.
        setExhaustedWindow(`${sid}::${refetchKey}`);
        return;
      }
      attemptsLeft -= 1;
      // A real attempt refreshes the idle budget, so a pending->ready->pending
      // sequence within one session still gets a full watch window.
      idleTicksLeft = PENDING_REPOLL_IDLE_TICKS;
      void doFetch(sid).finally(() => {
        if (cancelled) return;
        timer = setTimeout(tick, PENDING_REPOLL_MS);
      });
    };
    timer = setTimeout(tick, PENDING_REPOLL_MS);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // Re-arms on refetchKey (like the lag re-poll) so each new move opens a
    // fresh reconciliation window. NOT on scoreStatus — that is read through a
    // ref precisely so a status flip cannot re-arm the effect (finding C).
  }, [sessionId, refetchKey, doFetch]);

  // Prior data is shown ONLY for the same session: a session change yields []
  // synchronously, held until the new session's fetch commits. `scoreStatus` is
  // behind the SAME guard, so one game's pending state can never leak into the
  // next game's cards.
  const matches = state.sessionId === sessionId;
  return {
    lineage: matches ? state.lineage : EMPTY_LINEAGE,
    playerColor: matches ? state.playerColor : "white",
    startPly: matches ? state.startPly : 1,
    scoreStatus:
      matches &&
      state.scoreStatus === "pending" &&
      exhaustedWindow !== reconcileWindow
        ? "pending"
        : "ready",
  };
}
