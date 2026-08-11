import { useCallback, useEffect, useState } from "react";
import { useGameStore } from "../stores/useGameStore";
import { badgeFor, type OpeningDeltaBadge } from "../utils/openingDeltaBadge";

const TOAST_AUTO_DISMISS_MS = 6000;

export type LastDrillDeltaToast = {
  /** Identity of the notification. Acknowledgement is BY NONCE — acking by
   *  session could remove a later duplicate that was never shown. */
  nonce: number;
  /** `before` travels with the shared badge data; the toast renders its signed
   * diff and resolved score. */
  badges: Array<OpeningDeltaBadge & { openingName: string }>;
};

/**
 * Surface the head of the late-delta queue as a "last drill" toast (g-f3m4).
 *
 * A drill's diff can reconcile after the player has already started the next
 * one. It belongs to the drill that earned it, so it can never render as the
 * current drill's inline badges — but dropping it means the player never sees a
 * score change that really happened. This hook renders it as a separate,
 * explicitly-labelled notification.
 *
 * Deliberately NOT folded into useBoardNotice: that is a single-slot arbiter
 * topped by the review warning (which would suppress this), and its
 * `!isViewingLive` clause would drop the toast while the player scrubs history.
 */
export function useLastDrillDeltaToast(): {
  toast: LastDrillDeltaToast | null;
  dismiss: () => void;
} {
  const lateOpeningDeltas = useGameStore((s) => s.lateOpeningDeltas);
  const acknowledge = useGameStore((s) => s.acknowledgeLateOpeningDelta);
  const [dismissedNonce, setDismissedNonce] = useState<number | null>(null);

  const head = lateOpeningDeltas[0] ?? null;
  const isDismissed = head !== null && head.nonce === dismissedNonce;

  const toast: LastDrillDeltaToast | null =
    head && !isDismissed
      ? {
          nonce: head.nonce,
          badges: (head.items ?? []).flatMap((item) => {
            const badge = badgeFor(item);
            if (!badge) return [];
            return [{ ...badge, openingName: item.opening_name }];
          }),
        }
      : null;

  const dismiss = useCallback(() => {
    if (!head) return;
    setDismissedNonce(head.nonce);
    acknowledge(head.nonce);
  }, [head, acknowledge]);

  // Auto-dismiss. Keyed on the nonce, so a second queued notification gets its
  // own full window rather than inheriting the first one's remaining time.
  useEffect(() => {
    if (!toast) return;
    const nonce = toast.nonce;
    const timer = window.setTimeout(() => {
      setDismissedNonce(nonce);
      useGameStore.getState().acknowledgeLateOpeningDelta(nonce);
    }, TOAST_AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [toast?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  return { toast, dismiss };
}
