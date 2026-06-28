import { useEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";

// Shared timing constants — also imported by tests so assertions stay in sync
// with the actual animation timeline. Total lifecycle ≈ HOLD + SHRINK.
export const SRS_FAIL_HOLD_MS = 4500;
export const SRS_FAIL_SHRINK_MS = 520;

export type SrsFailTrigger = {
  id: number;
  moveIndex: number;
};

type SrsFailSpotlightProps = {
  // Nonce trigger: bump `id` to (re)start the spotlight. A new fail mid-display
  // restarts the 5s window cleanly.
  trigger: SrsFailTrigger | null;
  // The live chessboard square element to spotlight (the clip-path hole target).
  targetRef: RefObject<HTMLElement | null>;
  // Called when the spotlight finishes (auto or click-skip). Receives the id it
  // closed so the parent only clears the trigger if it still matches.
  onDone: (id: number) => void;
};

type Phase = "hidden" | "show" | "shrink";

const HOLE_PAD = 8;

function holeClipPath(r: DOMRect): string {
  const x1 = r.left - HOLE_PAD;
  const y1 = r.top - HOLE_PAD;
  const x2 = r.right + HOLE_PAD;
  const y2 = r.bottom + HOLE_PAD;
  // Outer viewport rect then an inner rect punched out via even-odd winding,
  // so the board shows through fully lit while everything else is dimmed.
  return `polygon(evenodd, 0 0, 100vw 0, 100vw 100vh, 0 100vh, 0 0, ${x1}px ${y1}px, ${x1}px ${y2}px, ${x2}px ${y2}px, ${x2}px ${y1}px, ${x1}px ${y1}px)`;
}

const SrsFailSpotlight = ({ trigger, targetRef, onDone }: SrsFailSpotlightProps) => {
  const [phase, setPhase] = useState<Phase>("hidden");
  const [rect, setRect] = useState<DOMRect | null>(null);
  const triggerId = trigger?.id ?? null;
  const onDoneRef = useRef(onDone);
  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  // Start / restart on a new trigger id: measure the board, re-measure on
  // resize + scroll (throttled via rAF), and schedule the hold -> shrink hop.
  useEffect(() => {
    if (triggerId === null) {
      return;
    }

    // Reset to the hold phase and measure the board for a fresh trigger. These
    // sync setStates are intentional — the spotlight syncs to an external DOM
    // measurement, not to React-derived state.
    /* eslint-disable react-hooks/set-state-in-effect */
    setPhase("show");

    const measure = () => {
      const el = targetRef.current;
      if (el) {
        setRect(el.getBoundingClientRect());
      }
    };
    measure();
    /* eslint-enable react-hooks/set-state-in-effect */

    let raf = 0;
    const onMove = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(measure);
    };
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);

    const holdTimer = window.setTimeout(() => setPhase("shrink"), SRS_FAIL_HOLD_MS);

    return () => {
      window.clearTimeout(holdTimer);
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [triggerId, targetRef]);

  // Once shrinking, unmount after the shrink animation and notify the parent.
  useEffect(() => {
    if (phase !== "shrink" || triggerId === null) {
      return;
    }
    const id = triggerId;
    const killTimer = window.setTimeout(() => {
      setPhase("hidden");
      onDoneRef.current(id);
    }, SRS_FAIL_SHRINK_MS);
    return () => window.clearTimeout(killTimer);
  }, [phase, triggerId]);

  if (phase === "hidden" || !rect || typeof document === "undefined") {
    return null;
  }

  const skip = () => setPhase("shrink");
  const shrinking = phase === "shrink";

  return createPortal(
    <>
      <div
        className={`srs-fail-scrim${shrinking ? " srs-fail-scrim--shrink" : ""}`}
        style={{ clipPath: holeClipPath(rect), WebkitClipPath: holeClipPath(rect) }}
        onClick={skip}
        aria-hidden="true"
      />
      <div
        className="srs-fail-content"
        style={{
          left: rect.left + rect.width / 2,
          top: rect.top + Math.min(18, rect.height * 0.04),
        }}
        onClick={skip}
        role="alert"
      >
        <div
          className={`srs-fail-content__inner${shrinking ? " srs-fail-content__inner--shrink" : ""}`}
        >
          <div className="srs-fail-content__glyph" aria-hidden="true">!</div>
          <h2 className="srs-fail-content__headline">You made this blunder again!</h2>
        </div>
      </div>
    </>,
    document.body,
  );
};

export default SrsFailSpotlight;
