let audioCtx: AudioContext | null = null;
let unlocked = false;

/**
 * Create and resume the AudioContext during a user gesture so the browser
 * allows audio playback. Called once from a document-level interaction
 * listener — subsequent playBling() calls reuse the unlocked context.
 */
function unlockAudio(): void {
  if (unlocked) return;
  unlocked = true;
  try {
    audioCtx = new AudioContext();
    if (audioCtx.state === "suspended") {
      void audioCtx.resume().catch(() => {});
    }
  } catch {
    // Web Audio not available
  }
}

// Register a one-shot listener so the context is created inside a genuine
// user gesture (click/keydown/touchstart). Browsers that gate Web Audio on
// activation will honour this because it runs synchronously in the event.
if (typeof document !== "undefined") {
  const events = ["click", "keydown", "touchstart"] as const;
  const handler = () => {
    unlockAudio();
    for (const evt of events) {
      document.removeEventListener(evt, handler, true);
    }
  };
  for (const evt of events) {
    document.addEventListener(evt, handler, { capture: true, once: false });
  }
}

export function playBling(): void {
  if (!audioCtx || audioCtx.state !== "running") return;

  const now = audioCtx.currentTime;
  const gain = audioCtx.createGain();
  gain.connect(audioCtx.destination);
  gain.gain.setValueAtTime(0.18, now);
  gain.gain.exponentialRampToValueAtTime(0.001, now + 0.25);

  // C6 → E6 two-tone bling
  const o1 = audioCtx.createOscillator();
  o1.type = "sine";
  o1.frequency.setValueAtTime(1047, now);
  o1.connect(gain);
  o1.start(now);
  o1.stop(now + 0.08);

  const o2 = audioCtx.createOscillator();
  o2.type = "sine";
  o2.frequency.setValueAtTime(1319, now + 0.08);
  o2.connect(gain);
  o2.start(now + 0.08);
  o2.stop(now + 0.2);
}
