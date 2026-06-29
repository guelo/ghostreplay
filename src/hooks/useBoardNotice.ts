import { useEffect, useRef, useState } from "react";
import type { BoardNotice, ResolvedReview } from "../components/chess-game/types";

type UseBoardNoticeArgs = {
  /** True while the player sits on a review FEN it's their turn to answer. */
  isReviewMomentActive: boolean;
  /** Pending/pass/fail of the most recent review move, or null. */
  resolvedReview: ResolvedReview | null;
  /**
   * Pre-gated rehook signal. The caller is responsible for AND-ing the raw
   * `showRehookToast` with `isGameActive && opponentMode === "ghost"` so this
   * hook only sees a rising edge it should actually surface.
   */
  showRehookNotice: boolean;
  /** False while scrubbing history; no notice should linger off-live. */
  isViewingLive: boolean;
};

/** How long each kind stays up before auto-dismissing. */
const DISMISS_MS: Record<BoardNotice["kind"], number> = {
  "review-warning": 4000, // "a few seconds"
  "review-result": 2000, // "shorter amount of time"
  rehook: 3000, // matches the prior toast behaviour
};

/**
 * Owns the single board notice slot. Collapses the old warning stack into one
 * box with a tiny priority model:
 *   - A new review warning always preempts (highest priority).
 *   - A pass/fail result shows once (never replays), even when the SRS-fail
 *     spotlight scrubs the board in the same commit that grades the move.
 *   - The rehook notice never stomps a review notice (lowest priority).
 * Each notice auto-dismisses on its own timer so it gets out of the player's way.
 */
export function useBoardNotice({
  isReviewMomentActive,
  resolvedReview,
  showRehookNotice,
  isViewingLive,
}: UseBoardNoticeArgs): BoardNotice | null {
  const [notice, setNotice] = useState<BoardNotice | null>(null);
  const nonceRef = useRef(0);

  // review-warning — both edges of isReviewMomentActive.
  const prevReviewActiveRef = useRef(isReviewMomentActive);
  useEffect(() => {
    const prev = prevReviewActiveRef.current;
    prevReviewActiveRef.current = isReviewMomentActive;
    if (!prev && isReviewMomentActive) {
      // Rising edge: always preempt whatever is showing.
      nonceRef.current += 1;
      setNotice({ kind: "review-warning", nonce: nonceRef.current });
    } else if (prev && !isReviewMomentActive) {
      // Falling edge: the player made the review move (or navigated away).
      // Pull a still-showing warning immediately instead of waiting out its
      // timer, so it's out of the way during the grading wait.
      setNotice((p) => (p?.kind === "review-warning" ? null : p));
    }
  }, [isReviewMomentActive]);

  // review-result — graded pass/fail. Keyed so a result is shown at most once
  // and never replays when isViewingLive flips back to true after grading.
  const lastResultKeyRef = useRef<string | null>(null);
  // isViewingLive as of the previous render. Latched in the off-live effect
  // below (declared after this one, so this effect reads the prior value).
  const prevIsViewingLiveRef = useRef(isViewingLive);
  useEffect(() => {
    const result = resolvedReview?.result;
    // Pending is never shown. The warning effect's falling-edge clear already
    // pulled any showing warning in the same commit the review move was made.
    if (result === "pending") return;
    if (result === "pass" || result === "fail") {
      const key = `${resolvedReview!.analysisId}:${result}`;
      if (key !== lastResultKeyRef.current) {
        lastResultKeyRef.current = key; // mark seen regardless of live state
        // Show when live, OR when the previous render was live — the latter
        // catches the SRS-fail spotlight, which scrubs the board (live -> not
        // live) in the SAME commit that grades the move. A result graded after
        // the player manually scrubbed away (off-live in the prior render too)
        // is marked seen but never shown.
        if (isViewingLive || prevIsViewingLiveRef.current) {
          nonceRef.current += 1;
          setNotice({ kind: "review-result", result, nonce: nonceRef.current });
        }
      }
    }
  }, [resolvedReview, isViewingLive]);

  // rehook — fill the slot only if nothing higher-priority is showing.
  const prevRehookRef = useRef(showRehookNotice);
  useEffect(() => {
    const prev = prevRehookRef.current;
    prevRehookRef.current = showRehookNotice;
    if (!prev && showRehookNotice) {
      nonceRef.current += 1;
      const nonce = nonceRef.current;
      setNotice((current) =>
        current && current.kind !== "rehook" ? current : { kind: "rehook", nonce },
      );
    }
  }, [showRehookNotice]);

  // Auto-dismiss — clear only if the same notice is still showing.
  useEffect(() => {
    if (!notice) return;
    const nonce = notice.nonce;
    const timer = setTimeout(() => {
      setNotice((p) => (p?.nonce === nonce ? null : p));
    }, DISMISS_MS[notice.kind]);
    return () => clearTimeout(timer);
  }, [notice]);

  // Latch isViewingLive for the next render (read by the result effect above).
  useEffect(() => {
    prevIsViewingLiveRef.current = isViewingLive;
  });

  // Hide a warning/rehook notice while scrubbing history (don't store the clear
  // in state — it's purely view-derived). A review result keeps showing: the
  // SRS-fail spotlight scrubs the board as part of *showing* the ✗ box.
  if (!isViewingLive && notice?.kind !== "review-result") {
    return null;
  }
  return notice;
}
