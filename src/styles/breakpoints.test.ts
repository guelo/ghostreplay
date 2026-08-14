import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { GAME_MOBILE_MAX_WIDTH } from "./breakpoints";

// Guard against TS/CSS breakpoint drift: the JS media query in
// AnalysisConnectors and the .game-page mobile layout must switch at the same
// width. During the mechanical CSS split, that layout block lives in the
// temporary legacy game-responsive partial. CSS can't import the constant, so
// assert the partial references the same pixel value. If you intentionally move
// the breakpoint, update both GAME_MOBILE_MAX_WIDTH and the matching @media
// block in the partial.
describe("game mobile breakpoint", () => {
  const gameResponsiveCssPath = path.resolve(
    process.cwd(),
    "src/styles/legacy/008-game-responsive.css",
  );
  const gameResponsiveCss = readFileSync(gameResponsiveCssPath, "utf8");

  it("matches the .game-page mobile @media block in the legacy partial", () => {
    const mobileBlock = new RegExp(
      `@media \\(max-width: ${GAME_MOBILE_MAX_WIDTH}px\\)`,
    );
    expect(gameResponsiveCss).toMatch(mobileBlock);
  });
});
