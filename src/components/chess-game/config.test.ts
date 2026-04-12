import { describe, expect, it } from "vitest";
import { GHOST_AVATAR_SRC, getOpponentAvatarSrc } from "./config";

describe("getOpponentAvatarSrc", () => {
  it("returns the exact asset for on-bin values", () => {
    expect(getOpponentAvatarSrc(600)).toBe("/images/gh600.png");
    expect(getOpponentAvatarSrc(800)).toBe("/images/gh800.png");
    expect(getOpponentAvatarSrc(1200)).toBe("/images/gh1200.png");
    expect(getOpponentAvatarSrc(1500)).toBe("/images/gh1500.png");
  });

  it("falls back to gh1500.png for stronger on-bin opponents", () => {
    expect(getOpponentAvatarSrc(1600)).toBe("/images/gh1500.png");
    expect(getOpponentAvatarSrc(2000)).toBe("/images/gh1500.png");
    expect(getOpponentAvatarSrc(2600)).toBe("/images/gh1500.png");
  });

  it("snaps off-bin values down to the nearest supported bin", () => {
    // 777 is between 600 and 800 → nearest bin <= 777 is 600.
    expect(getOpponentAvatarSrc(777)).toBe("/images/gh600.png");
    // 1250 is between 1200 and 1300 → 1200.
    expect(getOpponentAvatarSrc(1250)).toBe("/images/gh1200.png");
  });

  it("uses the smallest bin when input is below the lowest bin", () => {
    expect(getOpponentAvatarSrc(400)).toBe("/images/gh600.png");
    expect(getOpponentAvatarSrc(0)).toBe("/images/gh600.png");
  });

  it("exposes the ghost replay avatar path", () => {
    expect(GHOST_AVATAR_SRC).toBe("/branding/ghost-logo-option-1-buddy.svg");
  });
});
