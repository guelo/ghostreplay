import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("playMoveSound", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("plays move.m4a for a regular move", async () => {
    const play = vi.fn().mockResolvedValue(undefined);
    const instances: Array<{ src: string; currentTime: number }> = [];
    class AudioMock {
      src: string;
      preload = "";
      currentTime = 1;
      play = play;
      constructor(src: string) {
        this.src = src;
        instances.push(this);
      }
    }
    vi.stubGlobal("Audio", AudioMock);

    const { playMoveSound } = await import("./moveSound");
    playMoveSound(false);

    expect(instances[0]?.src).toBe("/audio/move.m4a");
    expect(instances[0]?.currentTime).toBe(0);
    expect(play).toHaveBeenCalledTimes(1);
  });

  it("plays take.m4a for a capture", async () => {
    const play = vi.fn().mockResolvedValue(undefined);
    const instances: Array<{ src: string }> = [];
    class AudioMock {
      src: string;
      preload = "";
      currentTime = 0;
      play = play;
      constructor(src: string) {
        this.src = src;
        instances.push(this);
      }
    }
    vi.stubGlobal("Audio", AudioMock);

    const { playMoveSound } = await import("./moveSound");
    playMoveSound(true);

    expect(instances[0]?.src).toBe("/audio/take.m4a");
    expect(play).toHaveBeenCalledTimes(1);
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
    setSoundMuted(true);
    setSoundVolume(0.2);

    const { playMoveSound } = await import("./moveSound");
    playMoveSound(false);

    expect(instances[0]?.muted).toBe(true);
    expect(instances[0]?.volume).toBe(0.2);
  });

  it("does not throw when Audio is undefined", async () => {
    vi.stubGlobal("Audio", undefined);
    const { playMoveSound } = await import("./moveSound");
    expect(() => playMoveSound(false)).not.toThrow();
  });

  it("does not throw when the Audio constructor throws synchronously", async () => {
    class AudioMock {
      constructor() {
        throw new Error("boom");
      }
    }
    vi.stubGlobal("Audio", AudioMock);
    const { playMoveSound } = await import("./moveSound");
    expect(() => playMoveSound(false)).not.toThrow();
  });

  it("does not throw when play() throws synchronously", async () => {
    class AudioMock {
      src: string;
      preload = "";
      currentTime = 0;
      constructor(src: string) {
        this.src = src;
      }
      play() {
        throw new Error("boom");
      }
    }
    vi.stubGlobal("Audio", AudioMock);
    const { playMoveSound } = await import("./moveSound");
    expect(() => playMoveSound(false)).not.toThrow();
  });

  it("swallows an async play() rejection", async () => {
    const play = vi.fn().mockRejectedValue(new Error("blocked"));
    class AudioMock {
      src: string;
      preload = "";
      currentTime = 0;
      play = play;
      constructor(src: string) {
        this.src = src;
      }
    }
    vi.stubGlobal("Audio", AudioMock);
    const { playMoveSound } = await import("./moveSound");
    expect(() => playMoveSound(false)).not.toThrow();
    await Promise.resolve();
  });
});
