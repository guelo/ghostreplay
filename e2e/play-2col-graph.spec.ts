import { expect, type Page } from "@playwright/test";
import { test } from "./fixtures/auth";

// Geometry checks for the /play 2-column layout (max-width:1099px, ≥660px).
// The analysis graph shrinks with viewport height — clamp(110px, 20dvh, 220px)
// — so it stays above the fold on short viewports while the panel may scroll
// off. See bead g-9g1y.

const boardSquare = (page: Page, square: string) =>
  page
    .locator(".chessboard-board-area")
    .locator(`[data-square="${square}"]`)
    .first();

const waitForMoveCountAtLeast = async (
  page: Page,
  minimum: number,
): Promise<void> => {
  await expect
    .poll(async () => page.locator(".move-list-grid .move-san").count())
    .toBeGreaterThanOrEqual(minimum);
};

const playMove = async (
  page: Page,
  from: string,
  to: string,
): Promise<void> => {
  await boardSquare(page, from).click();
  await boardSquare(page, to).click();
};

const startNewGameAsWhite = async (page: Page): Promise<void> => {
  await page
    .locator(".game-end-banner")
    .getByRole("button", { name: /new game/i })
    .click();
  const playWhiteButton = page.getByRole("button", { name: /play white/i });
  await expect(playWhiteButton).toBeVisible();
  await playWhiteButton.click();

  const playButton = page.getByRole("button", { name: /^play$/i });
  if (
    (await playButton.count()) > 0 &&
    (await playButton.first().isVisible())
  ) {
    await playButton.first().click();
  }

  await expect(page.locator(".game-status-badge--live")).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.locator(".chessboard-board-area")).toBeVisible();
};

const graphHeight = async (page: Page): Promise<number> => {
  const box = await page.locator(".analysis-graph").boundingBox();
  expect(box).not.toBeNull();
  return box!.height;
};

// Mid-game in the 2-col layout: the graph must stay fully in view as the
// viewport shrinks toward the ~600px target. Uses a non-seeded user so no
// review/rehook toasts perturb layout.
for (const { width, height } of [
  { width: 900, height: 761 },
  { width: 900, height: 681 },
  { width: 900, height: 600 },
]) {
  test(`2-col graph stays above the fold at ${width}x${height}`, async ({
    page,
    loginAs,
  }) => {
    await page.setViewportSize({ width, height });
    await loginAs(page, "stable");
    await page.goto("/game");

    await startNewGameAsWhite(page);
    await playMove(page, "e2", "e4");
    await waitForMoveCountAtLeast(page, 2);
    await playMove(page, "g1", "f3");
    await waitForMoveCountAtLeast(page, 4);

    const graph = page.locator(".analysis-graph");
    await expect(graph).toBeVisible();

    // Graph fully within the viewport (top ≥ 0, bottom ≤ viewport height).
    await expect
      .poll(async () => {
        const box = await graph.boundingBox();
        if (!box) return false;
        return box.y >= 0 && box.y + box.height <= height + 1;
      })
      .toBe(true);

    // Height tracks clamp(110px, 20dvh, 220px): floored at 110, capped at 220.
    const h = await graphHeight(page);
    const expected = Math.min(220, Math.max(110, 0.2 * height));
    expect(h).toBeGreaterThanOrEqual(110 - 2);
    expect(h).toBeLessThanOrEqual(220 + 2);
    expect(Math.abs(h - expected)).toBeLessThanOrEqual(8);
  });
}

// On short, wide 2-col viewports (681–760px tall, ≥768px wide) the board is
// capped by max-width while its column has horizontal slack, so reducing the
// reserved chrome grows the board into the dead margin and the graph lands at
// the viewport bottom. See g-jth2.
for (const { width, height } of [
  { width: 1000, height: 760 },
  { width: 1000, height: 700 },
  { width: 1000, height: 660 },
  { width: 1000, height: 600 },
]) {
  test(`2-col graph pins to the bottom when board is width-constrained at ${width}x${height}`, async ({
    page,
    loginAs,
  }) => {
    await page.setViewportSize({ width, height });
    await loginAs(page, "stable");
    await page.goto("/game");

    await startNewGameAsWhite(page);
    await playMove(page, "e2", "e4");
    await waitForMoveCountAtLeast(page, 2);

    const geom = await page.evaluate(() => {
      const graph = document
        .querySelector(".chess-graph-area")!
        .getBoundingClientRect();
      const board = document
        .querySelector(".chessboard-board-area")!
        .getBoundingClientRect();
      const column = document
        .querySelector(".chess-graph-area")!
        .getBoundingClientRect().width;
      return {
        graphBottom: graph.bottom,
        boardWidth: board.width,
        columnWidth: column,
        vh: window.innerHeight,
      };
    });

    // Board is width-constrained (narrower than its full-width graph column),
    // which is the precondition for the pin.
    expect(geom.boardWidth).toBeLessThan(geom.columnWidth);
    // Graph sits flush with the viewport bottom (within a small tolerance).
    expect(geom.vh - geom.graphBottom).toBeLessThanOrEqual(12);
    expect(geom.vh - geom.graphBottom).toBeGreaterThanOrEqual(-2);
  });
}

// The clamp is scoped to the 2-col media query and must not leak across
// breakpoints: desktop 3-col keeps the full 220px graph; mobile keeps 130px.
test("graph clamp does not leak across breakpoints", async ({
  page,
  loginAs,
}) => {
  await page.setViewportSize({ width: 1400, height: 1200 });
  await loginAs(page, "stable");
  await page.goto("/game");

  await startNewGameAsWhite(page);
  await playMove(page, "e2", "e4");
  await waitForMoveCountAtLeast(page, 2);
  await playMove(page, "g1", "f3");
  await waitForMoveCountAtLeast(page, 4);

  // Desktop 3-col (>1099w, tall): graph at its 220px cap.
  await expect(page.locator(".analysis-graph")).toBeVisible();
  expect(await graphHeight(page)).toBeCloseTo(220, -1);

  // Mobile (<660w): graph pinned to 130px by the mobile rule.
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".analysis-graph")).toBeVisible();
  await expect
    .poll(async () => Math.abs((await graphHeight(page)) - 130) <= 3)
    .toBe(true);
});
