import { BLUNDER_AUDIO_CLIPS } from "./config";
import { applyAudioSettings } from "../../utils/soundSettings";

export const playRandomBlunderAudio = () => {
  if (typeof Audio === "undefined" || BLUNDER_AUDIO_CLIPS.length === 0) {
    return;
  }
  const randomIndex = Math.floor(Math.random() * BLUNDER_AUDIO_CLIPS.length);
  const clip = BLUNDER_AUDIO_CLIPS[randomIndex];
  const audio = new Audio(clip);
  applyAudioSettings(audio);
  void audio.play().catch(() => {});
};
