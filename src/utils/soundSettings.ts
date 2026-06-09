// Persisted sound settings (mute + volume) with a three-layer source-of-truth:
//   1. In-memory snapshot below — the authoritative runtime value used by playback.
//   2. localStorage — persistence only, read once at init, written best-effort.
//   3. Zustand store (useGameStore) — UI reactive mirror.
// applyAudioSettings reads ONLY the snapshot so a storage failure can never make
// playback diverge from the UI.

const MUTED_STORAGE_KEY = "ghostreplay_sound_muted";
const VOLUME_STORAGE_KEY = "ghostreplay_sound_volume";

const DEFAULT_MUTED = false;
const DEFAULT_VOLUME = 1;

const clampVolume = (value: number): number => {
  if (!Number.isFinite(value)) return DEFAULT_VOLUME;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
};

export const readSoundMuted = (): boolean => {
  try {
    const value = window.localStorage.getItem(MUTED_STORAGE_KEY);
    if (value === "true") return true;
    if (value === "false") return false;
    return DEFAULT_MUTED;
  } catch {
    return DEFAULT_MUTED;
  }
};

export const readSoundVolume = (): number => {
  try {
    const raw = window.localStorage.getItem(VOLUME_STORAGE_KEY);
    // Number("") and Number("   ") coerce to 0; treat blank as missing/invalid.
    if (raw === null || raw.trim() === "") return DEFAULT_VOLUME;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return DEFAULT_VOLUME;
    return clampVolume(parsed);
  } catch {
    return DEFAULT_VOLUME;
  }
};

export const writeSoundMuted = (m: boolean): void => {
  try {
    window.localStorage.setItem(MUTED_STORAGE_KEY, m ? "true" : "false");
  } catch {
    // Persistence is best-effort.
  }
};

export const writeSoundVolume = (v: number): void => {
  try {
    window.localStorage.setItem(VOLUME_STORAGE_KEY, String(clampVolume(v)));
  } catch {
    // Persistence is best-effort.
  }
};

// Authoritative runtime snapshot, seeded from storage at module init.
let muted = readSoundMuted();
let volume = readSoundVolume();

export const getSoundMuted = (): boolean => muted;
export const getSoundVolume = (): number => volume;

/** Update snapshot + persist; returns the canonical (stored) value. */
export const setSoundMuted = (m: boolean): boolean => {
  muted = m;
  writeSoundMuted(muted);
  return muted;
};

/**
 * Update snapshot + persist; returns the canonical (clamped) value. Invalid
 * input (NaN/non-finite) retains the previous valid volume and returns it.
 */
export const setSoundVolume = (v: number): number => {
  if (!Number.isFinite(v)) return volume;
  volume = clampVolume(v);
  writeSoundVolume(volume);
  return volume;
};

/** Apply the snapshot to an audio element. Never throws into the caller. */
export const applyAudioSettings = (audio: HTMLAudioElement): void => {
  try {
    audio.muted = getSoundMuted();
    audio.volume = getSoundVolume();
  } catch {
    // Setting muted/volume should never break playback.
  }
};
