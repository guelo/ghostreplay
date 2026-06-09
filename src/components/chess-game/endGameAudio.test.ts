import { describe, expect, it, vi, afterEach } from "vitest";
import {
  getEndGameAudioClips,
  pickRandomClip,
  playEndGameAudio,
} from "./endGameAudio";
import type { EndGameAudioClips } from "./endGameAudio";

const clips: EndGameAudioClips = {
  win: ["/win-a.mp3", "/win-b.wav"],
  lose: ["/lose-a.mp3", "/lose-b.m4a"],
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("endGameAudio", () => {
  it("maps terminal results to the expected clip pools", () => {
    expect(
      getEndGameAudioClips({ type: "checkmate_win", message: "win" }, clips),
    ).toEqual(clips.win);
    expect(
      getEndGameAudioClips({ type: "checkmate_loss", message: "loss" }, clips),
    ).toEqual(clips.lose);
    expect(
      getEndGameAudioClips({ type: "resign", message: "resign" }, clips),
    ).toEqual(clips.lose);
    expect(
      getEndGameAudioClips({ type: "draw", message: "draw" }, clips),
    ).toEqual([]);
  });

  it("uses Math.random to select from the provided clips", () => {
    vi.spyOn(Math, "random").mockReturnValue(0.75);

    expect(pickRandomClip(clips.win)).toBe("/win-b.wav");
  });

  it("constructs and plays audio for a selected end-game clip", () => {
    const play = vi.fn().mockResolvedValue(undefined);
    const audioCtor = vi.fn();
    class MockAudio {
      constructor(src: string) {
        audioCtor(src);
      }

      play() {
        return play();
      }
    }
    vi.stubGlobal("Audio", MockAudio);
    vi.spyOn(Math, "random").mockReturnValue(0);

    playEndGameAudio({ type: "checkmate_loss", message: "lost" }, clips);

    expect(audioCtor).toHaveBeenCalledWith("/lose-a.mp3");
    expect(play).toHaveBeenCalledTimes(1);
  });

  it("applies muted/volume from settings before play", async () => {
    const play = vi.fn().mockResolvedValue(undefined);
    const instances: Array<{ muted: boolean; volume: number }> = [];
    class MockAudio {
      muted = false;
      volume = 1;
      constructor() {
        instances.push(this);
      }
      play() {
        return play();
      }
    }
    vi.stubGlobal("Audio", MockAudio);
    vi.spyOn(Math, "random").mockReturnValue(0);

    const { setSoundMuted, setSoundVolume } = await import(
      "../../utils/soundSettings"
    );
    setSoundMuted(true);
    setSoundVolume(0.15);

    playEndGameAudio({ type: "checkmate_loss", message: "lost" }, clips);

    expect(instances[0]?.muted).toBe(true);
    expect(instances[0]?.volume).toBe(0.15);
  });

  it("stays silent for draws", () => {
    const audioCtor = vi.fn();
    vi.stubGlobal("Audio", audioCtor);

    playEndGameAudio({ type: "draw", message: "draw" }, clips);

    expect(audioCtor).not.toHaveBeenCalled();
  });
});
