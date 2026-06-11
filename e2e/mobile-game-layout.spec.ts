import { expect, type Page } from "@playwright/test";
import { test } from "./fixtures/auth";

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
    .poll(async () => page.locator(".h-move-list__strip .h-move").count())
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

const playToSeededReviewPosition = async (page: Page): Promise<void> => {
  await playMove(page, "e2", "e4");
  await waitForMoveCountAtLeast(page, 2);
  await playMove(page, "g1", "f3");
  await waitForMoveCountAtLeast(page, 4);
  await playMove(page, "f1", "c4");
  await waitForMoveCountAtLeast(page, 6);
  await expect(
    page.locator(".chess-warning-stack--mobile .review-warning-toast"),
  ).toBeVisible();
};

const expectNoticesBelowBoard = async (page: Page): Promise<void> => {
  const boardBox = await page.locator(".chessboard-board-area").boundingBox();
  const warningBox = await page
    .locator(".chess-warning-stack--mobile")
    .boundingBox();
  expect(boardBox).not.toBeNull();
  expect(warningBox).not.toBeNull();
  expect(warningBox!.y).toBeGreaterThanOrEqual(boardBox!.y + boardBox!.height);
};

test("narrow game layout keeps controls usable and overlays in viewport", async ({
  page,
  loginAs,
}) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await loginAs(page, "due");
  await page.goto("/game");

  await startNewGameAsWhite(page);
  await playToSeededReviewPosition(page);

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

  const mobileWarnings = page.locator(".chess-warning-stack--mobile");
  await expect(mobileWarnings.locator(".review-warning-toast")).toBeVisible();
  await expect(mobileWarnings.locator(".rehook-toast")).toBeVisible();
  await expectNoticesBelowBoard(page);

  // Above the 659px portrait breakpoint the mobile notice stack is hidden and
  // notices move into the panel, so the below-board assertion no longer
  // applies. Resize only to exercise the ghost-info popover at a wider width.
  await page.setViewportSize({ width: 767, height: 430 });
  await expect(page.locator(".chess-warning-stack--mobile")).toBeHidden();

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
