import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { GAME_MOBILE_MAX_WIDTH } from "./breakpoints";

// Guard against TS/CSS breakpoint drift: the JS media query in
// AnalysisConnectors and the .game-page mobile layout must switch at the same
// width. The final CSS ownership split puts page chrome in GamePage.css and the
// game component layout in ChessGame.css. CSS can't import the constant, so
// assert both owners reference the same pixel value. If you intentionally move
// the breakpoint, update GAME_MOBILE_MAX_WIDTH and both matching @media blocks.
describe("game mobile breakpoint", () => {
  const ownerPaths = [
    "src/pages/GamePage.css",
    "src/components/chess-game/ChessGame.css",
  ];

  it.each(ownerPaths)("matches the mobile @media block in %s", (ownerPath) => {
    const ownerCss = readFileSync(path.resolve(process.cwd(), ownerPath), "utf8");
    const mobileBlock = new RegExp(
      `@media \\(max-width: ${GAME_MOBILE_MAX_WIDTH}px\\)`,
    );
    expect(ownerCss).toMatch(mobileBlock);
  });
});
