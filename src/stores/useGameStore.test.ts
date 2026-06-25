import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useGameStore } from "./useGameStore";

describe("useGameStore sound settings", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useGameStore.setState({ soundMuted: false, soundVolume: 1 });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("setSoundMuted persists and updates state", () => {
    const setItem = vi.spyOn(window.localStorage, "setItem");
    useGameStore.getState().setSoundMuted(true);

    expect(useGameStore.getState().soundMuted).toBe(true);
    expect(setItem).toHaveBeenCalledWith("ghostreplay_sound_muted", "true");
  });

  it("setSoundVolume persists and updates state", () => {
    const setItem = vi.spyOn(window.localStorage, "setItem");
    useGameStore.getState().setSoundVolume(0.5);

    expect(useGameStore.getState().soundVolume).toBe(0.5);
    expect(setItem).toHaveBeenCalledWith("ghostreplay_sound_volume", "0.5");
  });

  it("stores the canonical clamped value for out-of-range input", () => {
    useGameStore.getState().setSoundVolume(2);
    expect(useGameStore.getState().soundVolume).toBe(1);

    useGameStore.getState().setSoundVolume(-1);
    expect(useGameStore.getState().soundVolume).toBe(0);
  });

  it("retains the previous volume for invalid input", () => {
    useGameStore.getState().setSoundVolume(0.6);
    useGameStore.getState().setSoundVolume(Number.NaN);
    expect(useGameStore.getState().soundVolume).toBe(0.6);
  });

  it("supports updater functions", () => {
    useGameStore.getState().setSoundMuted((prev) => !prev);
    expect(useGameStore.getState().soundMuted).toBe(true);
  });
});

describe("useGameStore openingScoreChanges", () => {
  afterEach(() => {
    useGameStore.getState().setOpeningScoreChanges(null);
  });

  it("defaults to null and sets/clears the played-opening deltas", () => {
    expect(useGameStore.getState().openingScoreChanges).toBeNull();

    const changes = [
      {
        opening_key: "k1",
        opening_name: "Italian Game",
        opening_family: "Italian Game",
        eco: "C50",
        depth: 3,
        before: 41,
        after: 44,
        delta: 3,
        is_new: false,
      },
    ];
    useGameStore.getState().setOpeningScoreChanges(changes);
    expect(useGameStore.getState().openingScoreChanges).toEqual(changes);

    useGameStore.getState().setOpeningScoreChanges(null);
    expect(useGameStore.getState().openingScoreChanges).toBeNull();
  });
});
