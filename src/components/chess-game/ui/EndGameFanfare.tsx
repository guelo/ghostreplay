import { useEffect, useRef, useState } from "react";
import type { GameResult } from "../domain/status";
import { deriveEndGameAnnouncement } from "../domain/status";

// Shared timing constants — also imported by tests so assertions stay in sync
// with the actual animation timeline. Total lifecycle ≈ HOLD + SHRINK.
export const END_GAME_FANFARE_HOLD_MS = 2400;
export const END_GAME_FANFARE_SHRINK_MS = 450;

export type EndGameFanfareTrigger = {
  id: number;
  result: GameResult;
};

type EndGameFanfareProps = {
  // Nonce trigger: bump `id` to (re)start the fanfare. A new end mid-display
  // restarts the window cleanly.
  trigger: EndGameFanfareTrigger | null;
  // Called when the fanfare finishes (auto-dismiss or click-skip). Receives the
  // id it closed so the parent only clears the trigger if it still matches.
  onDone: (id: number) => void;
};

type Phase = "hidden" | "show" | "shrink";

/**
 * Brief, dramatic win/loss/draw card shown centered over the (already-dimmed)
 * board when a game genuinely ends (g-8079). Models SrsFailSpotlight's phase
 * machine — scale-in → hold → shrink-out, click-to-skip — but as a plain
 * board-area overlay (no portal / no clip-path measurement).
 */
const EndGameFanfare = ({ trigger, onDone }: EndGameFanfareProps) => {
  const [phase, setPhase] = useState<Phase>("hidden");
  const triggerId = trigger?.id ?? null;
  const onDoneRef = useRef(onDone);
  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  // Start / restart on a new trigger id: enter the hold phase and schedule the
  // hold -> shrink hop.
  useEffect(() => {
    if (triggerId === null) {
      return;
    }
    // This sync setState is intentional — the fanfare restarts on an external
    // nonce, not on React-derived state.
    /* eslint-disable-next-line react-hooks/set-state-in-effect */
    setPhase("show");
    const holdTimer = window.setTimeout(
      // Only advance from the hold phase. A click-skip (or a completed dismiss)
      // has already moved us to shrink/hidden; this effect is keyed on triggerId
      // and never re-runs on that phase change, so the timer survives. The guard
      // stops it from re-entering shrink — otherwise a stable-trigger parent
      // (e.g. BoardStage's default no-op onDone, which never clears the trigger)
      // would see the overlay flash back and onDone fire a second time.
      () => setPhase((p) => (p === "show" ? "shrink" : p)),
      END_GAME_FANFARE_HOLD_MS,
    );
    return () => window.clearTimeout(holdTimer);
  }, [triggerId]);

  // Once shrinking, unmount after the shrink animation and notify the parent.
  useEffect(() => {
    if (phase !== "shrink" || triggerId === null) {
      return;
    }
    const id = triggerId;
    const killTimer = window.setTimeout(() => {
      setPhase("hidden");
      onDoneRef.current(id);
    }, END_GAME_FANFARE_SHRINK_MS);
    return () => window.clearTimeout(killTimer);
  }, [phase, triggerId]);

  if (phase === "hidden" || !trigger) {
    return null;
  }

  const { outcome, headline, reason } = deriveEndGameAnnouncement(
    trigger.result,
  );
  const shrinking = phase === "shrink";
  const skip = () => setPhase("shrink");

  return (
    // The outer div is the live region (announces the result once on appear) and
    // the mouse "click anywhere to dismiss" surface. The inner card is a real
    // <button> so dismissal has a focus + native keyboard path (Enter/Space),
    // not mouse-only — while the button's text (not an aria-label) stays the
    // announced content so the polite live region still reads the outcome.
    <div
      className={`end-game-fanfare end-game-fanfare--${outcome}${shrinking ? " end-game-fanfare--shrink" : ""}`}
      onClick={skip}
      role="status"
      aria-live="polite"
    >
      <button
        type="button"
        className={`end-game-fanfare__inner${shrinking ? " end-game-fanfare__inner--shrink" : ""}`}
        onClick={skip}
      >
        <span className="end-game-fanfare__headline">{headline}</span>
        <span className="end-game-fanfare__reason">{reason}</span>
      </button>
    </div>
  );
};

export default EndGameFanfare;
