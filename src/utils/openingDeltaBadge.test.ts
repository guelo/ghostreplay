import { describe, expect, it } from "vitest";
import { badgeFor, hasRenderableBadge } from "./openingDeltaBadge";
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
  it("renders the rounded diff and direction", () => {
    expect(badgeFor(item({ before: 41, after: 44 }))).toEqual({
      diff: 3,
      after: 44,
      dir: "up",
    });
    expect(badgeFor(item({ before: 44, after: 41 }))).toEqual({
      diff: -3,
      after: 41,
      dir: "down",
    });
  });

  it("quantifies a brand-new opening against zero", () => {
    expect(badgeFor(item({ is_new: true, before: null, after: 37 }))).toEqual({
      diff: 37,
      after: 37,
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

  it("suppresses a sub-1.0 wobble that rounds to no change", () => {
    // The cards display ROUNDED scores, so a 0.4-point drift must never render
    // a misleading `+0` / `+1`.
    expect(badgeFor(item({ before: 44.1, after: 44.4 }))).toBeNull();
    expect(badgeFor(item({ before: 44, after: 44 }))).toBeNull();
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
