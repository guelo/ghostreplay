import type { GameResult } from "./domain/status";
import { applyAudioSettings } from "../../utils/soundSettings";

const winClipModules = import.meta.glob<string>(
  "../../assets/audio/win/*.{mp3,wav,m4a}",
  {
    eager: true,
    query: "?url",
    import: "default",
  },
);

const loseClipModules = import.meta.glob<string>(
  "../../assets/audio/lose/*.{mp3,wav,m4a}",
  {
    eager: true,
    query: "?url",
    import: "default",
  },
);

export const END_GAME_AUDIO_CLIPS = {
  win: Object.values(winClipModules),
  lose: Object.values(loseClipModules),
} as const;

export type EndGameAudioClips = {
  win: readonly string[];
  lose: readonly string[];
};

export const getEndGameAudioClips = (
  result: GameResult,
  clips: EndGameAudioClips = END_GAME_AUDIO_CLIPS,
): readonly string[] => {
  switch (result.type) {
    case "checkmate_win":
      return clips.win;
    case "checkmate_loss":
    case "resign":
      return clips.lose;
    case "draw":
      return [];
    default:
      return [];
  }
};

export const pickRandomClip = (clips: readonly string[]): string | null => {
  if (clips.length === 0) return null;
  return clips[Math.floor(Math.random() * clips.length)] ?? null;
};

export const playEndGameAudio = (
  result: GameResult,
  clips: EndGameAudioClips = END_GAME_AUDIO_CLIPS,
) => {
  if (typeof Audio === "undefined") return;

  const clip = pickRandomClip(getEndGameAudioClips(result, clips));
  if (!clip) return;

  try {
    const audio = new Audio(clip);
    applyAudioSettings(audio);
    void audio.play()?.catch?.(() => {});
  } catch {
    // Audio playback is best-effort and should never affect game finalization.
  }
};
