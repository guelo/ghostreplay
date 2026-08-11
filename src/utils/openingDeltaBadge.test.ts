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
  it("renders positive and negative changes from quantized tenths", () => {
    expect(badgeFor(item({ before: 41.6, after: 42.1 }))).toEqual({
      before: 41.6,
      diff: 0.5,
      after: 42.1,
      dir: "up",
    });
    expect(badgeFor(item({ before: 42.1, after: 41.6 }))).toEqual({
      before: 42.1,
      diff: -0.5,
      after: 41.6,
      dir: "down",
    });
  });

  it("quantifies a brand-new opening against zero", () => {
    expect(badgeFor(item({ is_new: true, before: null, after: 37.4 }))).toEqual({
      before: 0,
      diff: 37.4,
      after: 37.4,
      dir: "up",
    });
  });

  // The four suppression rules — each must render nothing, and (via
  // hasRenderableBadge) must not be queued as a late notification either.
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

  it("suppresses endpoints that quantize to the same tenth", () => {
    expect(badgeFor(item({ before: 44.11, after: 44.14 }))).toBeNull();
    expect(badgeFor(item({ before: 44, after: 44 }))).toBeNull();
  });

  it("derives the delta from integer tenths without floating-point drift", () => {
    expect(badgeFor(item({ before: 41.4, after: 41.6 }))).toMatchObject({
      before: 41.4,
      diff: 0.2,
      after: 41.6,
    });
  });
});

describe("terminal delta formatting", () => {
  it("always renders scores, deltas, and descriptions to one decimal place", () => {
    expect(formatOpeningDeltaValue(42)).toBe("42.0");
    expect(formatOpeningDeltaValue(-0.5)).toBe("-0.5");
    expect(
      describeOpeningDeltaBadge({
        before: 41.6,
        diff: 0.5,
        after: 42.1,
        dir: "up",
      }),
    ).toBe("Score increased by 0.5, now 42.1");
  });
});

describe("hasRenderableBadge", () => {
  it("is false for null, empty, and fully-suppressed payloads", () => {
    expect(hasRenderableBadge(null)).toBe(false);
    expect(hasRenderableBadge(undefined)).toBe(false);
    expect(hasRenderableBadge([])).toBe(false);
    expect(
      hasRenderableBadge([item({ after: null }), item({ before: 44, after: 44 })]),
    ).toBe(false);
  });

  it("is true when at least one entry would render", () => {
    expect(
      hasRenderableBadge([item({ after: null }), item({ before: 41, after: 44 })]),
    ).toBe(true);
  });
});
