import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { GAME_MOBILE_MAX_WIDTH } from "./breakpoints";

// Guard against TS/CSS breakpoint drift: the JS media query in
// AnalysisConnectors and the .game-page mobile layout in App.css must switch at
// the same width. CSS can't import the constant, so assert App.css references
// the same pixel value. If you intentionally move the breakpoint, update both
// GAME_MOBILE_MAX_WIDTH and the matching @media block in App.css.
describe("game mobile breakpoint", () => {
  const appCssPath = path.resolve(process.cwd(), "src/App.css");
  const appCss = readFileSync(appCssPath, "utf8");

  it("matches the .game-page mobile @media block in App.css", () => {
    const mobileBlock = new RegExp(
      `@media \\(max-width: ${GAME_MOBILE_MAX_WIDTH}px\\)`,
    );
    expect(appCss).toMatch(mobileBlock);
  });
});
