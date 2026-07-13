import { afterEach, describe, expect, it, vi } from "vitest";

import { MAIA_ELO_BINS } from "./config";
import { sampleDrillEloBin, sampleEloBin } from "./elo";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("sampleDrillEloBin", () => {
  it("maps the unit interval evenly onto every bin", () => {
    // One draw per bin, each landing mid-slice, so an off-by-one in the index
    // math would visibly shift the mapping rather than hide inside rounding.
    const drawn = MAIA_ELO_BINS.map((_, i) => {
      vi.spyOn(Math, "random").mockReturnValue(
        (i + 0.5) / MAIA_ELO_BINS.length,
      );
      return sampleDrillEloBin();
    });
    expect(drawn).toEqual([...MAIA_ELO_BINS]);
  });

  it("returns the bottom bin at 0 and the top bin at the upper limit", () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    expect(sampleDrillEloBin()).toBe(MAIA_ELO_BINS[0]);

    // Math.random() is [0, 1), but a stubbed 1 must not fall off the ladder.
    vi.spyOn(Math, "random").mockReturnValue(1);
    expect(sampleDrillEloBin()).toBe(MAIA_ELO_BINS[MAIA_ELO_BINS.length - 1]);
  });

  it("ignores the player's rating, unlike sampleEloBin", () => {
    // The whole point of g-acsr: a 1200 player must be able to draw the extreme
    // bins. Sweep the unit interval — the rating-centred Gaussian cannot reach
    // 600 or 2600 for any draw, and the uniform sampler reaches both. Draws
    // start above 0 because the Gaussian's cumulative walk degenerates to bin[0]
    // at exactly 0 (it subtracts the first weight before any comparison), which
    // says nothing about where its probability mass actually sits.
    const draws = Array.from({ length: 999 }, (_, i) => (i + 1) / 1000);
    const gaussian = new Set(
      draws.map((r) => {
        vi.spyOn(Math, "random").mockReturnValue(r);
        return sampleEloBin(1200) as number;
      }),
    );
    const uniform = new Set(
      draws.map((r) => {
        vi.spyOn(Math, "random").mockReturnValue(r);
        return sampleDrillEloBin() as number;
      }),
    );

    expect(gaussian.has(600)).toBe(false);
    expect(gaussian.has(2600)).toBe(false);
    expect(uniform.has(600)).toBe(true);
    expect(uniform.has(2600)).toBe(true);
  });

  it("reaches every bin over many unstubbed draws", () => {
    const seen = new Set<number>();
    for (let i = 0; i < 5000; i++) seen.add(sampleDrillEloBin());
    expect(seen.size).toBe(MAIA_ELO_BINS.length);
  });
});
