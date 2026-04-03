let audioCtx: AudioContext | null = null;
let sparkleBuffer: AudioBuffer | null = null;
let unlocked = false;

type BlingVariant = "mild" | "medium" | "bold";

type BlingPatch = {
  masterGain: number;
  arpeggioGain: number;
  attackGain: number;
  bellGain: number;
  sparkleGain: number;
  tailSeconds: number;
};

type OscillatorLayer = {
  type: OscillatorType;
  start: number;
  duration: number;
  frequency: number;
  peakGain: number;
  attackSeconds: number;
  detuneStart?: number;
  detuneEnd?: number;
  pitchEnd?: number;
  filterType?: BiquadFilterType;
  filterFrequency?: number;
  filterQ?: number;
  pan?: number;
};

const DEFAULT_BLING_VARIANT: BlingVariant = "medium";

const BLING_PATCHES: Record<BlingVariant, BlingPatch> = {
  mild: {
    masterGain: 0.11,
    arpeggioGain: 0.05,
    attackGain: 0.03,
    bellGain: 0.024,
    sparkleGain: 0.012,
    tailSeconds: 0.48,
  },
  medium: {
    masterGain: 0.15,
    arpeggioGain: 0.07,
    attackGain: 0.042,
    bellGain: 0.032,
    sparkleGain: 0.016,
    tailSeconds: 0.54,
  },
  bold: {
    masterGain: 0.19,
    arpeggioGain: 0.09,
    attackGain: 0.052,
    bellGain: 0.04,
    sparkleGain: 0.02,
    tailSeconds: 0.6,
  },
};

/**
 * Create and resume the AudioContext during a user gesture so the browser
 * allows audio playback. Called once from a document-level interaction
 * listener; subsequent playBling() calls reuse the unlocked context.
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

function getSparkleBuffer(ctx: AudioContext): AudioBuffer {
  if (sparkleBuffer && sparkleBuffer.sampleRate === ctx.sampleRate) {
    return sparkleBuffer;
  }

  const durationSeconds = 0.16;
  const frameCount = Math.max(1, Math.floor(ctx.sampleRate * durationSeconds));
  const buffer = ctx.createBuffer(1, frameCount, ctx.sampleRate);
  const channel = buffer.getChannelData(0);

  for (let i = 0; i < frameCount; i += 1) {
    const progress = i / frameCount;
    const decay = 1 - progress;
    channel[i] = (Math.random() * 2 - 1) * decay;
  }

  sparkleBuffer = buffer;
  return buffer;
}

function connectVoice(
  ctx: AudioContext,
  input: AudioNode,
  output: AudioNode,
  layer: Pick<OscillatorLayer, "filterType" | "filterFrequency" | "filterQ">,
): AudioNode {
  if (!layer.filterType || !layer.filterFrequency) {
    input.connect(output);
    return output;
  }

  const filter = ctx.createBiquadFilter();
  filter.type = layer.filterType;
  filter.frequency.setValueAtTime(layer.filterFrequency, ctx.currentTime);
  filter.Q.setValueAtTime(layer.filterQ ?? 0.7, ctx.currentTime);
  input.connect(filter);
  filter.connect(output);
  return filter;
}

function connectWithOptionalPan(
  ctx: AudioContext,
  input: AudioNode,
  output: AudioNode,
  pan?: number,
): void {
  if (pan == null || typeof ctx.createStereoPanner !== "function") {
    input.connect(output);
    return;
  }

  const panner = ctx.createStereoPanner();
  panner.pan.setValueAtTime(pan, ctx.currentTime);
  input.connect(panner);
  panner.connect(output);
}

function scheduleOscillatorLayer(
  ctx: AudioContext,
  destination: AudioNode,
  layer: OscillatorLayer,
): void {
  const oscillator = ctx.createOscillator();
  oscillator.type = layer.type;
  oscillator.frequency.setValueAtTime(layer.frequency, layer.start);
  oscillator.detune.setValueAtTime(layer.detuneStart ?? 0, layer.start);
  oscillator.detune.linearRampToValueAtTime(
    layer.detuneEnd ?? layer.detuneStart ?? 0,
    layer.start + layer.duration,
  );

  if (layer.pitchEnd && layer.pitchEnd > 0) {
    oscillator.frequency.linearRampToValueAtTime(
      layer.pitchEnd,
      layer.start + layer.duration,
    );
  }

  const voiceGain = ctx.createGain();
  voiceGain.gain.setValueAtTime(0.0001, layer.start);
  voiceGain.gain.linearRampToValueAtTime(
    layer.peakGain,
    layer.start + layer.attackSeconds,
  );
  voiceGain.gain.exponentialRampToValueAtTime(
    0.0001,
    layer.start + layer.duration,
  );

  connectVoice(ctx, oscillator, voiceGain, layer);
  connectWithOptionalPan(ctx, voiceGain, destination, layer.pan);

  oscillator.start(layer.start);
  oscillator.stop(layer.start + layer.duration);
}

function scheduleSparkleNoise(
  ctx: AudioContext,
  destination: AudioNode,
  start: number,
  patch: BlingPatch,
  options?: {
    pan?: number;
    gainScale?: number;
    playbackStart?: number;
    playbackEnd?: number;
    bandpassFrequency?: number;
    highpassFrequency?: number;
  },
): void {
  const source = ctx.createBufferSource();
  source.buffer = getSparkleBuffer(ctx);
  source.playbackRate.setValueAtTime(options?.playbackStart ?? 1.18, start);
  source.playbackRate.exponentialRampToValueAtTime(
    options?.playbackEnd ?? 0.82,
    start + 0.14,
  );

  const bandpass = ctx.createBiquadFilter();
  bandpass.type = "bandpass";
  bandpass.frequency.setValueAtTime(options?.bandpassFrequency ?? 6800, start);
  bandpass.Q.setValueAtTime(1.1, start);

  const highpass = ctx.createBiquadFilter();
  highpass.type = "highpass";
  highpass.frequency.setValueAtTime(options?.highpassFrequency ?? 3600, start);
  highpass.Q.setValueAtTime(0.7, start);

  const gain = ctx.createGain();
  const sparkleGain = patch.sparkleGain * (options?.gainScale ?? 1);
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.linearRampToValueAtTime(sparkleGain, start + 0.006);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.14);

  source.connect(bandpass);
  bandpass.connect(highpass);
  highpass.connect(gain);
  connectWithOptionalPan(ctx, gain, destination, options?.pan);

  source.start(start);
  source.stop(start + 0.16);
}

// Register a one-shot listener so the context is created inside a genuine
// user gesture (click/keydown/touchstart). Browsers that gate Web Audio on
// activation will honor this because it runs synchronously in the event.
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

export function playBling(variant: BlingVariant = DEFAULT_BLING_VARIANT): void {
  if (!audioCtx || audioCtx.state !== "running") return;

  const patch = BLING_PATCHES[variant];
  const now = audioCtx.currentTime;
  const master = audioCtx.createGain();
  master.connect(audioCtx.destination);
  master.gain.setValueAtTime(0.0001, now);
  master.gain.linearRampToValueAtTime(patch.masterGain, now + 0.012);
  master.gain.exponentialRampToValueAtTime(0.0001, now + patch.tailSeconds);

  scheduleOscillatorLayer(audioCtx, master, {
    type: "triangle",
    start: now,
    duration: 0.16,
    frequency: 1046.5,
    pitchEnd: 1174.66,
    peakGain: patch.arpeggioGain,
    attackSeconds: 0.012,
    detuneStart: 6,
    detuneEnd: 0,
    filterType: "lowpass",
    filterFrequency: 3600,
    filterQ: 0.5,
    pan: -0.04,
  });

  scheduleOscillatorLayer(audioCtx, master, {
    type: "triangle",
    start: now + 0.055,
    duration: 0.16,
    frequency: 1318.51,
    peakGain: patch.arpeggioGain * 0.84,
    attackSeconds: 0.01,
    detuneStart: 10,
    detuneEnd: -4,
    filterType: "lowpass",
    filterFrequency: 4100,
    filterQ: 0.65,
    pan: 0.04,
  });

  scheduleOscillatorLayer(audioCtx, master, {
    type: "sine",
    start: now + 0.11,
    duration: 0.18,
    frequency: 1567.98,
    peakGain: patch.arpeggioGain * 0.72,
    attackSeconds: 0.01,
    detuneStart: 5,
    detuneEnd: -3,
    filterType: "bandpass",
    filterFrequency: 2400,
    filterQ: 1.2,
    pan: 0.08,
  });

  scheduleOscillatorLayer(audioCtx, master, {
    type: "triangle",
    start: now,
    duration: 0.09,
    frequency: 2093,
    peakGain: patch.attackGain,
    attackSeconds: 0.004,
    detuneStart: 24,
    detuneEnd: -18,
    filterType: "highpass",
    filterFrequency: 1000,
    filterQ: 0.8,
    pan: -0.05,
  });

  scheduleOscillatorLayer(audioCtx, master, {
    type: "sine",
    start: now + 0.03,
    duration: 0.28,
    frequency: 3135.96,
    peakGain: patch.bellGain,
    attackSeconds: 0.008,
    detuneStart: 0,
    detuneEnd: -10,
    filterType: "bandpass",
    filterFrequency: 2900,
    filterQ: 3.5,
    pan: 0.12,
  });

  scheduleSparkleNoise(audioCtx, master, now + 0.008, patch, {
    pan: -0.24,
    playbackStart: 1.24,
    playbackEnd: 0.9,
    bandpassFrequency: 7200,
    highpassFrequency: 3800,
  });
  scheduleSparkleNoise(audioCtx, master, now + 0.034, patch, {
    pan: 0.28,
    gainScale: 0.78,
    playbackStart: 1.02,
    playbackEnd: 0.68,
    bandpassFrequency: 6200,
    highpassFrequency: 3400,
  });
}
