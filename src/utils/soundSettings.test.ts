import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("soundSettings", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("defaults to unmuted, full volume when storage is empty", async () => {
    const { getSoundMuted, getSoundVolume } = await import("./soundSettings");
    expect(getSoundMuted()).toBe(false);
    expect(getSoundVolume()).toBe(1);
  });

  it("clamps out-of-range stored volume on read", async () => {
    window.localStorage.setItem("ghostreplay_sound_volume", "5");
    const { getSoundVolume } = await import("./soundSettings");
    expect(getSoundVolume()).toBe(1);
  });

  it("rejects NaN/garbage stored volume and falls back to default", async () => {
    window.localStorage.setItem("ghostreplay_sound_volume", "not-a-number");
    const { getSoundVolume } = await import("./soundSettings");
    expect(getSoundVolume()).toBe(1);
  });

  it("treats an empty/whitespace stored volume as default, not zero", async () => {
    window.localStorage.setItem("ghostreplay_sound_volume", "   ");
    const { getSoundVolume } = await import("./soundSettings");
    expect(getSoundVolume()).toBe(1);
  });

  it("round-trips a write then read", async () => {
    const { setSoundVolume, setSoundMuted } = await import("./soundSettings");
    setSoundVolume(0.4);
    setSoundMuted(true);
    expect(window.localStorage.getItem("ghostreplay_sound_volume")).toBe("0.4");
    expect(window.localStorage.getItem("ghostreplay_sound_muted")).toBe("true");

    vi.resetModules();
    const fresh = await import("./soundSettings");
    expect(fresh.getSoundVolume()).toBe(0.4);
    expect(fresh.getSoundMuted()).toBe(true);
  });

  it("does not throw and falls back to defaults when getItem throws", async () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    const { getSoundMuted, getSoundVolume, setSoundVolume } = await import(
      "./soundSettings"
    );
    expect(getSoundMuted()).toBe(false);
    expect(getSoundVolume()).toBe(1);
    expect(() => setSoundVolume(0.5)).not.toThrow();
  });

  it("setSoundVolume clamps and returns the canonical value", async () => {
    const { setSoundVolume } = await import("./soundSettings");
    expect(setSoundVolume(2)).toBe(1);
    expect(setSoundVolume(-1)).toBe(0);
    expect(setSoundVolume(0.3)).toBe(0.3);
  });

  it("setSoundVolume retains previous value for invalid input", async () => {
    const { setSoundVolume } = await import("./soundSettings");
    setSoundVolume(0.6);
    expect(setSoundVolume(Number.NaN)).toBe(0.6);
    expect(setSoundVolume(Infinity)).toBe(0.6);
  });

  it("applyAudioSettings applies the snapshot to an audio element", async () => {
    const { setSoundMuted, setSoundVolume, applyAudioSettings } = await import(
      "./soundSettings"
    );
    setSoundMuted(true);
    setSoundVolume(0.25);
    const audio = { muted: false, volume: 1 } as HTMLAudioElement;
    applyAudioSettings(audio);
    expect(audio.muted).toBe(true);
    expect(audio.volume).toBe(0.25);
  });

  it("applyAudioSettings does not propagate a throwing audio object", async () => {
    const { applyAudioSettings } = await import("./soundSettings");
    const audio = {
      set muted(_v: boolean) {
        throw new Error("boom");
      },
    } as unknown as HTMLAudioElement;
    expect(() => applyAudioSettings(audio)).not.toThrow();
  });

  it("setters update the snapshot so applyAudioSettings reflects the change", async () => {
    const { setSoundVolume, applyAudioSettings } = await import(
      "./soundSettings"
    );
    setSoundVolume(0.5);
    const audio = { muted: false, volume: 1 } as HTMLAudioElement;
    applyAudioSettings(audio);
    expect(audio.volume).toBe(0.5);
  });
});
