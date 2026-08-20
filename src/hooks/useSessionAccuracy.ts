import { useEffect, useRef, useState } from "react";
import { ApiError, fetchAnalysis } from "../utils/api";

/**
 * Lifecycle of the post-game accuracy readout (g-frlfp).
 *
 *  - `idle`        — nothing requested (no ended session, or the surface is off).
 *  - `pending`     — the analysis payload is still being produced; show a placeholder.
 *  - `ready`       — settled with a number.
 *  - `unavailable` — settled with no number. `accuracy` fails CLOSED to null on a
 *                    non-mainline coordinate grid (backend/app/accuracy.py:96), so
 *                    this is "not measurable", never "scored zero" — callers must
 *                    hide the row rather than render a 0%.
 */
export type SessionAccuracyStatus = "idle" | "pending" | "ready" | "unavailable";

/**
 * Same cadence/budget as GameAnalysisPage (src/pages/GameAnalysisPage.tsx:11),
 * which polls the same endpoint for the same completion signal. Kept as literals
 * rather than imported so the page's UI concerns can diverge without silently
 * retuning this poll.
 */
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60;

type AccuracyState = {
  sessionId: string | null;
  accuracy: number | null;
  status: SessionAccuracyStatus;
};

const IDLE: AccuracyState = { sessionId: null, accuracy: null, status: "idle" };

/**
 * Read `summary.accuracy` for one ENDED session, polling `/session/{id}/analysis`
 * until the payload reports `is_complete`.
 *
 * The payload is produced move-by-move after the game ends, so the first response
 * usually carries a partial (or null) accuracy. That partial value is deliberately
 * NOT surfaced: the row stays `pending` until the analysis completes (or the budget
 * runs out), so the number never visibly jumps while the player reads it.
 *
 * Session-scoped by construction: state carries the session it was fetched for and
 * a mismatch reads as `idle`, so one game's accuracy can never leak into the next.
 * The fetch effect is keyed on (sessionId, enabled) and skips a session it has
 * already settled, so at most one poll sequence runs per ended session no matter
 * how often the banner mounts or the surface is toggled off and back on.
 */
export function useSessionAccuracy(
  sessionId: string | null,
  enabled: boolean,
): { accuracy: number | null; status: SessionAccuracyStatus } {
  const [state, setState] = useState<AccuracyState>(IDLE);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Sessions this hook has already answered for. `enabled` falls and rises again
  // within one ended game — "New Game" hides the prompt, cancelling the overlay
  // restores it — and the analysis for a session is immutable once complete, so
  // re-running the poll would only spend a request to re-derive the same number,
  // with a chance of a transient failure downgrading a known accuracy to
  // `unavailable`.
  const settledRef = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled || sessionId == null) return;
    const sid = sessionId;
    if (settledRef.current === sid) return;

    let cancelled = false;
    let attempts = 0;

    const settle = (accuracy: number | null) => {
      settledRef.current = sid;
      setState({
        sessionId: sid,
        accuracy,
        status: accuracy == null ? "unavailable" : "ready",
      });
    };

    const schedule = () => {
      attempts += 1;
      timerRef.current = setTimeout(() => {
        if (!cancelled) doFetch();
      }, POLL_INTERVAL_MS);
    };

    const doFetch = () => {
      fetchAnalysis(sid)
        .then((data) => {
          if (cancelled) return;
          if (data.is_complete) {
            settle(data.summary.accuracy);
            return;
          }
          if (attempts < POLL_MAX_ATTEMPTS) {
            schedule();
            return;
          }
          // Budget exhausted on a payload that never completed. Whatever the last
          // response carried is the best answer that will ever exist for it.
          settle(data.summary.accuracy);
        })
        .catch((err) => {
          if (cancelled) return;
          const isPermanent = err instanceof ApiError && !err.retryable;
          if (!isPermanent && attempts < POLL_MAX_ATTEMPTS) {
            schedule();
            return;
          }
          settle(null);
        });
    };

    doFetch();

    return () => {
      cancelled = true;
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [sessionId, enabled]);

  if (!enabled || sessionId == null) {
    return { accuracy: null, status: "idle" };
  }
  // A session change reads as `pending` on the SAME tick the id changes — the
  // previous game's number must never be attributed to the new one, not even for
  // a frame, and the effect below has a fetch armed for the new one. Deriving the
  // opening state (rather than seeding it from the effect) also keeps the effect
  // free of a synchronous setState.
  if (state.sessionId !== sessionId) {
    return { accuracy: null, status: "pending" };
  }
  return { accuracy: state.accuracy, status: state.status };
}
