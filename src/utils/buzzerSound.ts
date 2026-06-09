import { applyAudioSettings } from "./soundSettings";

const BUZZER_AUDIO_CLIP = "/audio/buzzer.mp3";

let buzzerAudio: HTMLAudioElement | null = null;

function getBuzzerAudio(): HTMLAudioElement | null {
  if (typeof Audio === "undefined") {
    return null;
  }
  if (!buzzerAudio) {
    buzzerAudio = new Audio(BUZZER_AUDIO_CLIP);
    buzzerAudio.preload = "auto";
  }
  return buzzerAudio;
}

export function playBuzzer(): void {
  const audio = getBuzzerAudio();
  if (!audio) {
    return;
  }

  applyAudioSettings(audio);
  audio.currentTime = 0;
  void audio.play().catch(() => {});
}
