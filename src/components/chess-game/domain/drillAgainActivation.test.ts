import { describe, expect, it } from "vitest";
import { classifyDrillAgainInput } from "./drillAgainActivation";

describe("classifyDrillAgainInput", () => {
  it("classifies positive-detail pointer and touch-style activations as pointer", () => {
    expect(classifyDrillAgainInput({ detail: 1, isTrusted: false })).toBe(
      "pointer",
    );
  });

  it("classifies trusted zero-detail activation as keyboard", () => {
    expect(classifyDrillAgainInput({ detail: 0, isTrusted: true })).toBe(
      "keyboard",
    );
  });

  it("keeps element.click and missing events in the programmatic bucket", () => {
    expect(classifyDrillAgainInput({ detail: 0, isTrusted: false })).toBe(
      "programmatic",
    );
    expect(classifyDrillAgainInput()).toBe("programmatic");
  });
});
