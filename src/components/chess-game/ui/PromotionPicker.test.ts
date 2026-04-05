import { describe, it, expect } from "vitest";
import { squareToPercent } from "./PromotionPicker";

describe("squareToPercent", () => {
  it("white promoting e8, white orientation", () => {
    expect(squareToPercent("e8", "white")).toEqual({ left: 50, top: 0 });
  });

  it("white promoting e8, black orientation", () => {
    expect(squareToPercent("e8", "black")).toEqual({ left: 37.5, top: 87.5 });
  });

  it("black promoting d1, white orientation", () => {
    expect(squareToPercent("d1", "white")).toEqual({ left: 37.5, top: 87.5 });
  });

  it("black promoting d1, black orientation", () => {
    expect(squareToPercent("d1", "black")).toEqual({ left: 50, top: 0 });
  });
});
