import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("playMoveSound", () => {
  beforeEach(() => {
    vi.resetModules();
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
      preload = "";
      currentTime = 0;
      constructor(public src: string) {}
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
      preload = "";
      currentTime = 0;
      play = play;
      constructor(public src: string) {}
    }
    vi.stubGlobal("Audio", AudioMock);
    const { playMoveSound } = await import("./moveSound");
    expect(() => playMoveSound(false)).not.toThrow();
    await Promise.resolve();
  });
});
