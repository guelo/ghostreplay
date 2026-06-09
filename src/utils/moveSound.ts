import { applyAudioSettings } from "./soundSettings";

const MOVE_AUDIO_CLIP = "/audio/move.m4a";
const TAKE_AUDIO_CLIP = "/audio/take.m4a";

let moveAudio: HTMLAudioElement | null = null;
let takeAudio: HTMLAudioElement | null = null;

function getMoveAudio(isCapture: boolean): HTMLAudioElement | null {
  if (typeof Audio === "undefined") {
    return null;
  }
  if (isCapture) {
    if (!takeAudio) {
      takeAudio = new Audio(TAKE_AUDIO_CLIP);
      takeAudio.preload = "auto";
    }
    return takeAudio;
  }
  if (!moveAudio) {
    moveAudio = new Audio(MOVE_AUDIO_CLIP);
    moveAudio.preload = "auto";
  }
  return moveAudio;
}

export function playMoveSound(isCapture: boolean): void {
  try {
    const audio = getMoveAudio(isCapture);
    if (!audio) {
      return;
    }
    applyAudioSettings(audio);
    audio.currentTime = 0;
    void audio.play()?.catch?.(() => {});
  } catch {
    // Move audio is best-effort and should never affect the move commit path.
  }
}
