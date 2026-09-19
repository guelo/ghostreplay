import { describe, expect, it } from "vitest";
import {
  badgeFor,
  describeOpeningDeltaBadge,
  formatOpeningDeltaValue,
  hasRenderableBadge,
} from "./openingDeltaBadge";
import type { OpeningScoreDeltaItem } from "./api";

const item = (
  overrides: Partial<OpeningScoreDeltaItem>,
): OpeningScoreDeltaItem => ({
  opening_key: "k1",
  opening_name: "Italian Game",
  opening_family: "Italian Game",
  eco: "C50",
  depth: 3,
  before: 41,
  after: 44,
  delta: 3,
  is_new: false,
  ...overrides,
});

describe("badgeFor", () => {
  it("renders positive and negative changes from rounded whole numbers", () => {
    expect(badgeFor(item({ before: 41.4, after: 42.6 }))).toEqual({
      before: 41,
      diff: 2,
      after: 43,
      dir: "up",
    });
    expect(badgeFor(item({ before: 42.6, after: 41.4 }))).toEqual({
      before: 43,
      diff: -2,
      after: 41,
      dir: "down",
    });
  });

  it("quantifies a brand-new opening against zero", () => {
    expect(badgeFor(item({ is_new: true, before: null, after: 37.4 }))).toEqual({
      before: 0,
      diff: 37,
      after: 37,
      dir: "up",
    });
  });

  // The four suppression rules — each must render nothing, and (via
  // hasRenderableBadge) must also be reported as unrenderable in telemetry.
  it("suppresses a missing change", () => {
    expect(badgeFor(undefined)).toBeNull();
    expect(badgeFor(null)).toBeNull();
  });

  it("suppresses a null after-score", () => {
    expect(badgeFor(item({ after: null }))).toBeNull();
  });

  it("suppresses a non-new entry with no baseline", () => {
    expect(badgeFor(item({ is_new: false, before: null }))).toBeNull();
  });

  it("suppresses endpoints that round to the same whole number", () => {
    expect(badgeFor(item({ before: 41.6, after: 42.1 }))).toBeNull();
    expect(badgeFor(item({ before: 42.1, after: 41.6 }))).toBeNull();
    expect(badgeFor(item({ is_new: true, before: null, after: 0.49 }))).toBeNull();
    expect(badgeFor(item({ before: 44, after: 44 }))).toBeNull();
  });

  it("shows a whole-number change even when the raw delta rounds to zero", () => {
    expect(badgeFor(item({ before: 41.49, after: 41.5, delta: 0.01 }))).toEqual({
      before: 41,
      diff: 1,
      after: 42,
      dir: "up",
    });
    expect(badgeFor(item({ before: 41.5, after: 41.49, delta: -0.01 }))).toEqual({
      before: 42,
      diff: -1,
      after: 41,
      dir: "down",
    });
  });
});

describe("terminal delta formatting", () => {
  it("renders scores, deltas, and descriptions without decimals", () => {
    expect(formatOpeningDeltaValue(42)).toBe("42");
    expect(formatOpeningDeltaValue(41.5)).toBe("42");
    expect(formatOpeningDeltaValue(-1)).toBe("-1");
    expect(
      describeOpeningDeltaBadge({
        before: 41,
        diff: 1,
        after: 42,
        dir: "up",
      }),
    ).toBe("Score increased by 1, now 42");
  });
});

describe("hasRenderableBadge", () => {
  it("is false for null, empty, and fully-suppressed payloads", () => {
    expect(hasRenderableBadge(null)).toBe(false);
    expect(hasRenderableBadge(undefined)).toBe(false);
    expect(hasRenderableBadge([])).toBe(false);
    expect(
      hasRenderableBadge([item({ after: null }), item({ before: 41.6, after: 42.1 })]),
    ).toBe(false);
  });

  it("is true when at least one entry would render", () => {
    expect(
      hasRenderableBadge([item({ after: null }), item({ before: 41, after: 44 })]),
    ).toBe(true);
  });
});
