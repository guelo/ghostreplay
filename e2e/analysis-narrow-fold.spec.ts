import { expect, type Page } from "@playwright/test";
import { test } from "./fixtures/auth";

/**
 * Geometry checks for the narrow (<=720px) analysis-board fold budget on
 * /history and /game.
 *
 * Stacked single-column used to size the board off the viewport WIDTH, so a
 * square board pushed the eval graph and the game-review stats below the fold.
 * Two modes now share the vertical budget (see the "Narrow fold budget" block
 * in App.css):
 *
 *   mode 1 (>=811px tall) — still stacked, board capped off viewport height so
 *     the graph plus >=75% of the stats pane stay above the fold.
 *   mode 2 (<=810px tall) — where mode 1 would drive the board under 250px, the
 *     stats pane moves BESIDE the graph so the row costs max(graph, stats)
 *     instead of graph + stats, buying the board back its height.
 *
 * Assertions are geometric contracts (visible / beside / centered), not pixel
 * values, so type and copy tweaks don't churn them.
 */

const box = async (page: Page, selector: string) => {
  const b = await page.locator(selector).boundingBox();
  expect(b, `expected ${selector} to be laid out`).not.toBeNull();
  return b!;
};

/** Land on the seeded game analysis for the stable user. */
const openHistory = async (page: Page): Promise<void> => {
  await page.goto("/history");
  await expect(page.locator(".analysis-board")).toBeVisible();
  await expect(page.locator(".history-stats-pane")).toBeVisible();
};

const openGame = async (page: Page): Promise<void> => {
  const apiURL = process.env.E2E_API_URL ?? "http://127.0.0.1:8010";
  await page.goto("/history");
  const token = await page.evaluate(() =>
    localStorage.getItem("ghost_replay_token"),
  );
  const res = await page.request.get(`${apiURL}/api/history?limit=1`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const data = (await res.json()) as { games?: { session_id?: string }[] };
  const sessionId = data?.games?.[0]?.session_id ?? null;
  expect(sessionId, "seeded stable user must have a game in history").toBeTruthy();
  await page.goto(`/game?id=${sessionId}`);
  await expect(page.locator(".analysis-board")).toBeVisible();
  await expect(page.locator(".history-stats-pane")).toBeVisible();
};

const PAGES = [
  { name: "history", open: openHistory },
  { name: "game", open: openGame },
];

// --- Mode 1: stacked, graph + >=75% of the stats pane above the fold --------

for (const target of PAGES) {
  for (const { width, height } of [
    { width: 720, height: 900 },
    { width: 500, height: 900 },
    { width: 390, height: 844 },
  ]) {
    test(`${target.name}: stacked narrow keeps graph and 75% of stats above the fold at ${width}x${height}`, async ({
      page,
      loginAs,
    }) => {
      await page.setViewportSize({ width, height });
      await loginAs(page, "stable");
      await target.open(page);

      const graph = await box(page, ".analysis-graph");
      const stats = await box(page, ".history-stats-pane");
      const board = await box(page, ".analysis-board__board-frame");
      const col = await box(page, ".analysis-board__board-col");

      // Still stacked: the stats pane sits under the graph, not beside it.
      expect(stats.y).toBeGreaterThanOrEqual(graph.y + graph.height);

      // Graph fully in view.
      expect(graph.y).toBeGreaterThanOrEqual(0);
      expect(graph.y + graph.height).toBeLessThanOrEqual(height + 1);

      // At least 75% of the stats pane in view.
      const statsVisible = Math.min(stats.y + stats.height, height) - stats.y;
      expect(statsVisible / stats.height).toBeGreaterThanOrEqual(0.75);

      // The board gave up the width to pay for it, and is centered in the
      // column rather than pinned to the leading edge.
      expect(board.width).toBeLessThan(width);
      const leading = col.x;
      const trailing = width - (col.x + col.width);
      expect(Math.abs(leading - trailing)).toBeLessThanOrEqual(2);

      // Mode 1 only holds while it leaves a usable board (mode 2 takes over).
      expect(board.width).toBeGreaterThanOrEqual(250);
    });
  }
}

// --- Mode 2: stats beside the graph on short viewports ----------------------

for (const target of PAGES) {
  for (const { width, height } of [
    { width: 500, height: 800 },
    { width: 390, height: 734 },
    { width: 360, height: 640 },
  ]) {
    test(`${target.name}: short narrow puts stats beside the graph at ${width}x${height}`, async ({
      page,
      loginAs,
    }) => {
      await page.setViewportSize({ width, height });
      await loginAs(page, "stable");
      await target.open(page);

      const graph = await box(page, ".analysis-graph");
      const stats = await box(page, ".history-stats-pane");
      const board = await box(page, ".analysis-board__board-frame");

      // Side by side: stats start after the graph ends, on the same row.
      expect(stats.x).toBeGreaterThanOrEqual(graph.x + graph.width);
      expect(Math.abs(stats.y - graph.y)).toBeLessThanOrEqual(2);

      // Both fully in view.
      expect(graph.y + graph.height).toBeLessThanOrEqual(height + 1);
      expect(stats.y + stats.height).toBeLessThanOrEqual(height + 1);
      expect(stats.x + stats.width).toBeLessThanOrEqual(width + 1);

      // The graph keeps the leftover width — the stats pane is the narrow one.
      expect(graph.width).toBeGreaterThan(0);
      expect(stats.width).toBeLessThan(graph.width + stats.width);

      // Trading the stacked stats row for a side-by-side one is what buys the
      // board its height back; it must be bigger than mode 1 would have left.
      expect(board.width).toBeGreaterThan(150);
    });
  }
}

// --- The narrow rules must not leak above 720px -----------------------------

test("wide viewports keep their own board sizing and stats layout", async ({
  page,
  loginAs,
}) => {
  await page.setViewportSize({ width: 900, height: 900 });
  await loginAs(page, "stable");
  await openHistory(page);

  const graph = await box(page, ".analysis-graph");
  const stats = await box(page, ".history-stats-pane");
  const board = await box(page, ".analysis-board__board-frame");
  const col = await box(page, ".analysis-board__board-col");

  // 721–1099px is the pre-existing two-column layout, untouched by the narrow
  // rules: the board is height-driven (100dvh - 24rem, far larger than the
  // narrow cap) and sits at the leading edge beside the move list rather than
  // being centered.
  // Comfortably above the ~338px the narrow cap would allow at this height.
  expect(board.width).toBeGreaterThan(450);
  const leading = col.x;
  const trailing = 900 - (col.x + col.width);
  expect(trailing).toBeGreaterThan(leading + 100);

  // The stats footer already rides beside the graph in this band.
  expect(stats.x).toBeGreaterThanOrEqual(graph.x + graph.width);
});
