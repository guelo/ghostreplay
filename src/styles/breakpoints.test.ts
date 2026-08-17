import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { GAME_MOBILE_MAX_WIDTH } from "./breakpoints";

// Guard against TS/CSS breakpoint drift: the JS media query in
// AnalysisConnectors and the .game-page mobile layout must switch at the same
// width. ChessGame.css owns the component layout contract; GamePage.css only
// follows it for qualified route chrome. CSS can't import the constant, so
// assert the final component owner references the same pixel value. If you
// intentionally move the breakpoint, update GAME_MOBILE_MAX_WIDTH and the
// matching owner block.
describe("game mobile breakpoint", () => {
  const ownerPath = "src/components/chess-game/ChessGame.css";

  it("matches the final ChessGame.css mobile @media block", () => {
    const ownerCss = readFileSync(path.resolve(process.cwd(), ownerPath), "utf8");
    const mobileBlock = new RegExp(
      `@media \\(max-width: ${GAME_MOBILE_MAX_WIDTH}px\\)`,
    );
    expect(ownerCss).toMatch(mobileBlock);
  });
});
