import { expect, type Page } from "@playwright/test";
import { test } from "./fixtures/auth";
import { waitForMoveCountAtLeast as waitForCount } from "./fixtures/moves";

const boardSquare = (page: Page, square: string) =>
  page
    .locator(".chessboard-board-area")
    .locator(`[data-square="${square}"]`)
    .first();

const waitForMoveCountAtLeast = async (
  page: Page,
  minimum: number,
): Promise<void> => {
  await waitForCount(page.locator(".h-move-list__strip .h-move"), minimum);
};

const playMove = async (
  page: Page,
  from: string,
  to: string,
): Promise<void> => {
  await boardSquare(page, from).click();
  await boardSquare(page, to).click();
};

const dragMove = async (
  page: Page,
  from: string,
  to: string,
): Promise<void> => {
  const fromBox = await boardSquare(page, from).boundingBox();
  const toBox = await boardSquare(page, to).boundingBox();
  if (!fromBox || !toBox) throw new Error("Expected draggable board squares");
  await page.mouse.move(
    fromBox.x + fromBox.width / 2,
    fromBox.y + fromBox.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(toBox.x + toBox.width / 2, toBox.y + toBox.height / 2, {
    steps: 8,
  });
  await page.mouse.up();
};

const startNewGameAsWhite = async (page: Page): Promise<void> => {
  await expect(page.locator(".chessboard-board-area")).toBeVisible();
  const playWhiteButton = page.getByRole("button", { name: /play white/i });
  if (!(await playWhiteButton.isVisible())) {
    await page
      .locator(".game-end-banner")
      .getByRole("button", { name: /new game/i })
      .click();
  }
  await expect(playWhiteButton).toBeVisible();
  await playWhiteButton.click();

  await expect(page.locator(".game-status-badge--live")).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.locator(".chessboard-board-area")).toBeVisible();
};

const playToSeededReviewPosition = async (page: Page): Promise<void> => {
  // Exercise actual pointer dragging for the first live move; later moves keep
  // the click path covered as well.
  await dragMove(page, "e2", "e4");
  await waitForMoveCountAtLeast(page, 2);
  await playMove(page, "g1", "f3");
  await waitForMoveCountAtLeast(page, 4);
  await playMove(page, "f1", "c4");
  await waitForMoveCountAtLeast(page, 6);
  // The review warning now renders on the board itself (top-left), not in a
  // below-board stack. It auto-dismisses after a few seconds, so assert it
  // promptly within its window.
  await expect(
    page.locator(".chessboard-board-area .board-notice--review-warning"),
  ).toBeVisible();
};

test("mobile navigation exposes its named menu button", async ({
  page,
  loginAs,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await loginAs(page, "stable");
  await page.goto("/play");

  await expect(
    page.getByRole("button", { name: "Open navigation menu" }),
  ).toBeVisible();
});

test("play setup stays usable in a short landscape viewport", async ({
  page,
  loginAs,
}) => {
  const viewport = { width: 667, height: 375 };
  await page.setViewportSize(viewport);
  await loginAs(page, "stable");
  await page.goto("/game");

  const panel = page.locator(
    ".chess-start-panel:not(.chess-start-panel--drill)",
  );
  const modeToggle = panel.locator(".mode-toggle-row");
  const playActions = panel.locator(".chess-start-options");
  const scrollRegion = panel.locator(".chess-start-scroll");

  await expect(panel).toBeVisible();
  await expect(modeToggle).toBeVisible();
  await expect(playActions).toBeVisible();
  await expect(page.getByRole("button", { name: /play white/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /play random/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /play black/i })).toBeVisible();

  const panelBox = await panel.boundingBox();
  expect(panelBox).not.toBeNull();
  expect(panelBox!.y).toBeGreaterThanOrEqual(0);
  expect(panelBox!.y + panelBox!.height).toBeLessThanOrEqual(viewport.height);

  for (const fixedControl of [modeToggle, playActions]) {
    const controlBox = await fixedControl.boundingBox();
    expect(controlBox).not.toBeNull();
    expect(controlBox!.y).toBeGreaterThanOrEqual(panelBox!.y);
    expect(controlBox!.y + controlBox!.height).toBeLessThanOrEqual(
      panelBox!.y + panelBox!.height,
    );
  }

  const canScroll = await scrollRegion.evaluate(
    (element) => element.scrollHeight > element.clientHeight,
  );
  expect(canScroll).toBe(true);

  await scrollRegion.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect
    .poll(() => scrollRegion.evaluate((element) => element.scrollTop))
    .toBeGreaterThan(0);

  // The side choice remains available while the setup fields scroll.
  await expect(playActions).toBeVisible();
});

test("narrow game layout keeps controls usable and overlays in viewport", async ({
  page,
  loginAs,
}) => {
  test.slow();
  await page.setViewportSize({ width: 360, height: 740 });
  await loginAs(page, "due");
  await page.goto("/game");

  await startNewGameAsWhite(page);
  await playToSeededReviewPosition(page);

  // Park on an earlier ply so the on-board return affordance appears. On the
  // narrow board it announces itself, then tucks to approximately one square.
  await page.locator(".h-move-list__strip .h-move").first().click();
  const returnToLive = page.locator("button.board-return-live");
  await expect(returnToLive).toBeVisible();
  await expect(returnToLive).toHaveClass(/board-return-live--tucked/, {
    timeout: 3_000,
  });

  const compactBox = await returnToLive.boundingBox();
  const narrowBoardBox = await page.locator(".chessboard-square-measure").boundingBox();
  expect(compactBox).not.toBeNull();
  expect(narrowBoardBox).not.toBeNull();
  const narrowSquareSize = narrowBoardBox!.width / 8;
  expect(compactBox!.width).toBeLessThanOrEqual(narrowSquareSize * 1.15);
  expect(compactBox!.height).toBeLessThanOrEqual(narrowSquareSize * 0.575);

  // The same mounted review session expands back to the labeled form when the
  // board has enough room, then return to live before continuing layout checks.
  await page.setViewportSize({ width: 900, height: 700 });
  await expect(page.locator(".chessboard-square-measure")).toHaveCSS(
    "width",
    /4\d\dpx|[5-9]\d\dpx/,
  );
  await expect(returnToLive).not.toHaveClass(/board-return-live--tucked/);
  await expect(returnToLive.locator(".board-return-live__label")).toBeVisible();
  const labeledBox = await returnToLive.boundingBox();
  expect(labeledBox).not.toBeNull();
  expect(labeledBox!.height).toBeLessThanOrEqual(22);
  await returnToLive.click();
  await page.setViewportSize({ width: 360, height: 740 });

  const movesColumn = page.locator(".moves-column");
  const graphArea = page.locator(".chess-graph-area");
  const controlsRow = page.locator(".moves-column .controls-row");
  const moveStripRow = page.locator(".h-move-list__row");

  await expect(movesColumn).toBeVisible();
  await expect(graphArea).toBeVisible();
  await expect(controlsRow).toBeVisible();
  await expect(moveStripRow).toBeVisible();

  const movesBox = await movesColumn.boundingBox();
  const graphBox = await graphArea.boundingBox();
  const controlsBox = await controlsRow.boundingBox();
  const stripBox = await moveStripRow.boundingBox();

  expect(movesBox).not.toBeNull();
  expect(graphBox).not.toBeNull();
  expect(controlsBox).not.toBeNull();
  expect(stripBox).not.toBeNull();
  expect(movesBox!.y).toBeLessThan(graphBox!.y);
  // Controls row sits above the move strip in the horizontal list.
  expect(controlsBox!.y).toBeLessThan(stripBox!.y);

  // Resize to a wider width to exercise the ghost-info popover positioning.
  await page.setViewportSize({ width: 767, height: 430 });

  await page.getByRole("button", { name: "Toggle ghost info" }).click();
  const ghostInfo = page.locator(".ghost-info-box");
  await expect(ghostInfo).toBeVisible();
  const ghostBox = await ghostInfo.boundingBox();
  const viewport = page.viewportSize();
  expect(ghostBox).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(ghostBox!.x).toBeGreaterThanOrEqual(0);
  expect(ghostBox!.x + ghostBox!.width).toBeLessThanOrEqual(viewport!.width);
});

// Toast-free mid-game: after playing a couple of moves the analysis graph
// should be in the viewport once the header auto-scrolls away. Uses a plain
// (non-seeded) user so no review/rehook toasts add a row and push the graph
// down. Poll the graph box (not the wrapper) to absorb the async smooth scroll.
for (const { width, height } of [
  { width: 390, height: 844 },
  { width: 360, height: 640 },
]) {
  test(`analysis graph is in view mid-game at ${width}x${height}`, async ({
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

    await expect
      .poll(async () => {
        const box = await graph.boundingBox();
        if (!box) return false;
        return box.y < height && box.y + box.height > 0;
      })
      .toBe(true);
  });
}
