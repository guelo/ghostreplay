import { describe, expect, it } from "vitest";
import { buildEngineArrows, engineArrowColor } from "./AnalysisBoard.helpers";
import type { EngineInfo } from "../workers/stockfishMessages";

const BEST_MOVE_COLOR = "rgba(59, 130, 246, 1.00)";
const DEFAULT_BLUE = "rgba(59, 130, 246, 0.45)";

const line = (
  uci: string,
  score?: EngineInfo["score"],
): EngineInfo => ({
  pv: [uci],
  score,
  depth: 20,
});

describe("buildEngineArrows", () => {
  it("returns empty array for no lines", () => {
    expect(buildEngineArrows([])).toEqual([]);
  });

  it("single line is always blue", () => {
    const arrows = buildEngineArrows(
      [line("e2e4", { type: "cp", value: 30 })],
    );
    expect(arrows).toHaveLength(1);
    expect(arrows[0].color).toBe(BEST_MOVE_COLOR);
  });

  describe("cp-loss ordering", () => {
    it("best move is solid blue, others are faded blue with decreasing opacity", () => {
      const lines: EngineInfo[] = [
        line("e2e4", { type: "cp", value: 30 }),
        line("d2d4", { type: "cp", value: 20 }),
        line("c2c4", { type: "cp", value: -50 }),
      ];
      const arrows = buildEngineArrows(lines);

      expect(arrows).toHaveLength(3);
      expect(arrows[0].color).toBe(BEST_MOVE_COLOR);
      // Both non-best arrows should be blue
      expect(arrows[1].color).toMatch(/^rgba\(59, 130, 246,/);
      expect(arrows[2].color).toMatch(/^rgba\(59, 130, 246,/);
      // Larger loss → lower opacity
      const opacity1 = parseFloat(arrows[1].color.match(/[\d.]+\)$/)![0]);
      const opacity2 = parseFloat(arrows[2].color.match(/[\d.]+\)$/)![0]);
      expect(opacity1).toBeGreaterThan(opacity2);
    });
  });

  describe("mate-vs-cp ordering", () => {
    it("mate best move is blue, cp lines are faded blue at minimum opacity", () => {
      const lines: EngineInfo[] = [
        line("e2e4", { type: "mate", value: 3 }),
        line("d2d4", { type: "cp", value: 200 }),
        line("c2c4", { type: "cp", value: 50 }),
      ];
      const arrows = buildEngineArrows(lines);

      expect(arrows[0].color).toBe(BEST_MOVE_COLOR);
      expect(arrows[1].color).toMatch(/^rgba\(59, 130, 246,/);
      expect(arrows[2].color).toMatch(/^rgba\(59, 130, 246,/);
      // Loss relative to mate is so large both cp lines hit minimum opacity
      const opacity1 = parseFloat(arrows[1].color.match(/[\d.]+\)$/)![0]);
      const opacity2 = parseFloat(arrows[2].color.match(/[\d.]+\)$/)![0]);
      expect(opacity1).toBe(0.05);
      expect(opacity2).toBe(0.05);
    });

    it("two mate lines get correct relative styling", () => {
      const lines: EngineInfo[] = [
        line("e2e4", { type: "mate", value: 1 }),
        line("d2d4", { type: "mate", value: 5 }),
      ];
      const arrows = buildEngineArrows(lines);

      expect(arrows[0].color).toBe(BEST_MOVE_COLOR);
      // mate-in-5 is worse than mate-in-1, so faded blue
      expect(arrows[1].color).toMatch(/^rgba\(59, 130, 246,/);
    });
  });

  it("live line scoring higher than cached best stays at max opacity", () => {
    const lines: EngineInfo[] = [
      line("e2e4", { type: "cp", value: 30 }),  // cached best
      line("d2d4", { type: "cp", value: 50 }),  // live line evaluates higher
    ];
    const arrows = buildEngineArrows(lines);

    expect(arrows[0].color).toBe(BEST_MOVE_COLOR);
    // Negative cpLoss must be clamped — alternate opacity caps at 0.75
    const opacity = parseFloat(arrows[1].color.match(/[\d.]+\)$/)![0]);
    expect(opacity).toBeLessThanOrEqual(0.75);
    expect(arrows[1].color).toBe("rgba(59, 130, 246, 0.75)");
  });

  describe("missing-score fallback", () => {
    it("scoreless first line is blue, scored later lines are faded blue", () => {
      const lines: EngineInfo[] = [
        line("e2e4"), // cached best, no score
        line("d2d4", { type: "cp", value: 20 }),
        line("c2c4", { type: "cp", value: -10 }),
      ];
      const arrows = buildEngineArrows(lines);

      expect(arrows[0].color).toBe(BEST_MOVE_COLOR);
      expect(arrows[1].color).toMatch(/^rgba\(59, 130, 246,/);
      expect(arrows[2].color).toMatch(/^rgba\(59, 130, 246,/);
    });

    it("all scoreless lines: first solid blue, rest default blue", () => {
      const lines: EngineInfo[] = [
        line("e2e4"),
        line("d2d4"),
        line("c2c4"),
      ];
      const arrows = buildEngineArrows(lines);

      expect(arrows[0].color).toBe(BEST_MOVE_COLOR);
      expect(arrows[1].color).toBe(DEFAULT_BLUE);
      expect(arrows[2].color).toBe(DEFAULT_BLUE);
    });
  });

  it("deduplicates arrows with same start/end squares", () => {
    const lines: EngineInfo[] = [
      line("e2e4", { type: "cp", value: 30 }),
      line("e2e4", { type: "cp", value: 20 }),
    ];
    const arrows = buildEngineArrows(lines);
    expect(arrows).toHaveLength(1);
  });

  it("skips lines without pv", () => {
    const lines: EngineInfo[] = [
      { score: { type: "cp", value: 30 }, depth: 20 },
      line("e2e4", { type: "cp", value: 20 }),
    ];
    const arrows = buildEngineArrows(lines);
    // The first line (no pv) is skipped; second becomes index 1 but
    // since it's the first arrow emitted after index 0 is skipped,
    // it's NOT index 0 — it uses its actual array index for scoring.
    expect(arrows).toHaveLength(1);
  });
});

describe("engineArrowColor", () => {
  it("returns max opacity (0.75) for 0 cp loss", () => {
    expect(engineArrowColor(0)).toBe("rgba(59, 130, 246, 0.75)");
  });

  it("returns min opacity for large cp loss", () => {
    expect(engineArrowColor(500)).toBe("rgba(59, 130, 246, 0.05)");
  });

  it("scales opacity between bounds", () => {
    const color = engineArrowColor(30);
    const opacity = parseFloat(color.match(/[\d.]+\)$/)![0]);
    expect(opacity).toBeGreaterThan(0.05);
    expect(opacity).toBeLessThan(0.75);
  });

  it("clamps negative cpLoss so opacity never exceeds 0.75", () => {
    const color = engineArrowColor(-50);
    const opacity = parseFloat(color.match(/[\d.]+\)$/)![0]);
    expect(opacity).toBeLessThanOrEqual(0.75);
    expect(engineArrowColor(-50)).toBe("rgba(59, 130, 246, 0.75)");
  });
});
