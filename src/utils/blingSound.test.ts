import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("playBling", () => {
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
      src: string;
      preload = "";
      currentTime = 1;
      muted = false;
      volume = 1;
      play = play;
      constructor(src: string) {
        this.src = src;
        instances.push(this);
      }
    }
    vi.stubGlobal("Audio", AudioMock);

    const { setSoundMuted, setSoundVolume } = await import("./soundSettings");
    setSoundMuted(false);
    setSoundVolume(0.7);

    const { playBling } = await import("./blingSound");
    playBling();

    expect(instances[0]?.muted).toBe(false);
    expect(instances[0]?.volume).toBe(0.7);
    expect(play).toHaveBeenCalledTimes(1);
  });
});
