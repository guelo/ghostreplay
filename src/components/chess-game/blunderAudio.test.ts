import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("playRandomBlunderAudio", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("applies muted/volume from settings before play", async () => {
    const play = vi.fn().mockResolvedValue(undefined);
    const instances: Array<{ muted: boolean; volume: number }> = [];
    class AudioMock {
      muted = false;
      volume = 1;
      play = play;
      constructor() {
        instances.push(this);
      }
    }
    vi.stubGlobal("Audio", AudioMock);

    const { setSoundMuted, setSoundVolume } = await import(
      "../../utils/soundSettings"
    );
    setSoundMuted(true);
    setSoundVolume(0.35);

    const { playRandomBlunderAudio } = await import("./blunderAudio");
    playRandomBlunderAudio();

    expect(instances[0]?.muted).toBe(true);
    expect(instances[0]?.volume).toBe(0.35);
    expect(play).toHaveBeenCalledTimes(1);
  });
});
