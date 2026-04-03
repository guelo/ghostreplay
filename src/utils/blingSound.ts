const BEST_MOVE_AUDIO_CLIP = "/audio/bestmove.mp3";

let bestMoveAudio: HTMLAudioElement | null = null;

function getBestMoveAudio(): HTMLAudioElement | null {
  if (typeof Audio === "undefined") {
    return null;
  }
  if (!bestMoveAudio) {
    bestMoveAudio = new Audio(BEST_MOVE_AUDIO_CLIP);
    bestMoveAudio.preload = "auto";
  }
  return bestMoveAudio;
}

export function playBling(): void {
  const audio = getBestMoveAudio();
  if (!audio) {
    return;
  }

  audio.currentTime = 0;
  void audio.play().catch(() => {});
}
