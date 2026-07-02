import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchSessionOpenings,
  type OpeningLineageItem,
  type OpeningPlayerColor,
} from "../utils/api";

interface UseSessionOpeningsOptions {
  /** Bump to force a refetch (live move count, or analysis move count). */
  refetchKey: number;
  /** When set together with `active`, poll on this interval to converge the
   *  upload->display lag. Omit (or `active=false`) to disable polling. */
  pollIntervalMs?: number;
  /** Gate polling — only poll while the game is active. */
  active?: boolean;
}

interface SessionOpeningsState {
  sessionId: string | null;
  lineage: OpeningLineageItem[];
  playerColor: OpeningPlayerColor;
  startPly: number;
}

// Stable empty reference so a session change / disabled hook does not churn
// renders via a fresh array each time.
const EMPTY_LINEAGE: OpeningLineageItem[] = [];

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
 *  - With `pollIntervalMs` + `active`, a sequential poll (never setInterval, never
 *    a synchronous fetch) converges the lag for plies that upload after their
 *    local move event. The poll effect cannot fetch — only re-arm — so toggling
 *    `active` never triggers a data fetch (finding C).
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
  { refetchKey, pollIntervalMs, active = false }: UseSessionOpeningsOptions,
): {
  lineage: OpeningLineageItem[];
  playerColor: OpeningPlayerColor;
  startPly: number;
} {
  const [state, setState] = useState<SessionOpeningsState>({
    sessionId: null,
    lineage: EMPTY_LINEAGE,
    playerColor: "white",
    startPly: 1,
  });

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

  // Poll while active. Fires ONLY on a timer tick (never synchronously),
  // re-arming after each request settles so requests never overlap. Tearing down
  // (active false / dep change / unmount) cancels the cycle so no further ticks.
  useEffect(() => {
    if (sessionId == null || !active || !pollIntervalMs) return;
    const sid = sessionId;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = () => {
      if (cancelled) return;
      void doFetch(sid).finally(() => {
        if (cancelled) return;
        timer = setTimeout(tick, pollIntervalMs);
      });
    };
    timer = setTimeout(tick, pollIntervalMs);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [sessionId, refetchKey, active, pollIntervalMs, doFetch]);

  // Prior data is shown ONLY for the same session: a session change yields []
  // synchronously, held until the new session's fetch commits.
  const matches = state.sessionId === sessionId;
  return {
    lineage: matches ? state.lineage : EMPTY_LINEAGE,
    playerColor: matches ? state.playerColor : "white",
    startPly: matches ? state.startPly : 1,
  };
}
