import { expect, type Page } from "@playwright/test";
import { apiURL } from "./env";
import { test } from "./fixtures/auth";

/**
 * Geometry checks for the narrow (<=720px) analysis-board fold budget on
 * /history and /game.
 *
 * Stacked single-column used to size the board off the viewport WIDTH, so a
 * square board pushed the eval graph and the game-review stats below the fold.
 * Two modes now share the vertical budget (see the "Narrow fold budget" block
 * in AnalysisBoard.css):
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

type Box = { x: number; y: number; width: number; height: number };

/**
 * Measure several elements in ONE layout pass, once the layout has stopped
 * moving.
 *
 * Two `locator.boundingBox()` calls are two round-trips, so a layout that
 * settles between them yields a torn read: numbers from two different layout
 * states that no single frame ever rendered. Every assertion here compares
 * boxes against each other, so a torn read fails a contract the page never
 * actually broke.
 *
 * That is not hypothetical (g-fold-e2e-flake): on load the wide band settles
 * ~100ms in — a sub-pixel 100dvh change re-resolves the graph's
 * `calc(100% - 250px)` cap, which moves the stats pane by 46px. The old
 * per-box reads straddled that settle under the full parallel run and failed
 * `stats.x >= graph.x + graph.width` with the graph measured before it and the
 * stats after.
 *
 * The single `evaluate` removes the tearing; polling for two identical
 * consecutive reads makes the assertions describe the settled layout instead of
 * a frame on the way there.
 *
 * It also has to re-earn the two guarantees `locator.boundingBox()` gave for
 * free, because `getBoundingClientRect()` gives neither: a `display:none`
 * element yields a truthy all-zeros rect rather than null, and `querySelector`
 * silently takes the first of N matches where a locator is strict. So each
 * selector must resolve to exactly ONE visible, non-empty element — otherwise a
 * hidden or ambiguous graph would sail through these assertions. Filtering to
 * the visible matches before counting keeps that strictness without banning a
 * responsive layout that renders a variant it hides by CSS.
 */
type Measured = { box?: Box; problem?: string };

const settledBoxes = async <K extends string>(
  page: Page,
  selectors: Record<K, string>,
): Promise<Record<K, Box>> => {
  const read = () =>
    page.evaluate((sels: Record<string, string>) => {
      const out: Record<string, Measured> = {};
      for (const [name, selector] of Object.entries(sels)) {
        const rendered = [...document.querySelectorAll(selector)]
          .filter((el) =>
            el.checkVisibility({
              contentVisibilityAuto: true,
              visibilityProperty: true,
            }),
          )
          .map((el) => el.getBoundingClientRect())
          .filter((r) => r.width > 0 && r.height > 0);

        if (rendered.length !== 1) {
          out[name] = {
            problem: `${selector} matched ${rendered.length} visible non-empty elements (expected exactly 1)`,
          };
          continue;
        }
        const r = rendered[0];
        out[name] = {
          box: { x: r.x, y: r.y, width: r.width, height: r.height },
        };
      }
      return out;
    }, selectors);

  let latest: Record<string, Measured> = {};
  let previous = "";
  await expect
    .poll(
      async () => {
        latest = await read();
        const problems = Object.values(latest)
          .map((m) => m.problem)
          .filter(Boolean);
        const current = JSON.stringify(latest);
        const unchanged = current === previous;
        previous = current;
        if (problems.length > 0) return problems.join("; ");
        return unchanged ? "settled" : "still moving";
      },
      {
        message: `expected ${Object.values(selectors).join(", ")} to be laid out and settled`,
        // Fixed cadence, not expect.poll's default [100, 250, 500, ...]: the
        // quiet window has to be WIDER than the transient it must not mistake
        // for the settled layout. The wide band holds its pre-settle values for
        // ~130ms, so two reads 100ms apart can both land inside it and match.
        // 250ms cannot fit in that plateau.
        intervals: [250],
        timeout: 10_000,
      },
    )
    .toBe("settled");

  return Object.fromEntries(
    Object.entries(latest).map(([name, m]) => [name, (m as Measured).box]),
  ) as Record<K, Box>;
};

/** Land on the seeded game analysis for the stable user. */
const openHistory = async (page: Page): Promise<void> => {
  await page.goto("/history");
  await expect(page.locator(".analysis-board")).toBeVisible();
  await expect(page.locator(".history-stats-pane")).toBeVisible();
};

const openGame = async (page: Page): Promise<void> => {
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

      const { graph, stats, board, col } = await settledBoxes(page, {
        graph: ".analysis-graph",
        stats: ".history-stats-pane",
        board: ".analysis-board__board-frame",
        col: ".analysis-board__board-col",
      });

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
    { width: 320, height: 568 },
  ]) {
    test(`${target.name}: short narrow puts stats beside the graph at ${width}x${height}`, async ({
      page,
      loginAs,
    }) => {
      await page.setViewportSize({ width, height });
      await loginAs(page, "stable");
      await target.open(page);

      const { graph, plot, stats, board } = await settledBoxes(page, {
        graph: ".analysis-graph",
        plot: ".analysis-graph > svg",
        stats: ".history-stats-pane",
        board: ".analysis-board__board-frame",
      });

      // Side by side: stats start after the graph ends, on the same row.
      expect(stats.x).toBeGreaterThanOrEqual(graph.x + graph.width);
      expect(Math.abs(stats.y - graph.y)).toBeLessThanOrEqual(2);

      // Both fully in view.
      expect(graph.y + graph.height).toBeLessThanOrEqual(height + 1);
      expect(stats.y + stats.height).toBeLessThanOrEqual(height + 1);
      expect(stats.x + stats.width).toBeLessThanOrEqual(width + 1);

      // The direct-child SVG is the actual plotted data area; the graph wrapper
      // also includes the fixed-width y-axis. The plot must win the width budget
      // rather than merely leaving a nonzero wrapper beside the stats pane.
      expect(plot.width).toBeGreaterThan(stats.width);

      // Trading the stacked stats row for a side-by-side one is what buys the
      // board its height back; it must be bigger than mode 1 would have left.
      expect(board.width).toBeGreaterThan(150);

      if (width <= 360) {
        await page
          .getByRole("button", { name: /what does accuracy mean/i })
          .click();
        const { popup } = await settledBoxes(page, {
          popup: ".history-stats-pane__info-popup",
        });
        expect(popup.x).toBeGreaterThanOrEqual(-1);
        expect(popup.x + popup.width).toBeLessThanOrEqual(width + 1);
        expect(popup.y).toBeGreaterThanOrEqual(-1);
        expect(popup.y + popup.height).toBeLessThanOrEqual(height + 1);
      }
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

  const { graph, stats, board, col } = await settledBoxes(page, {
    graph: ".analysis-graph",
    stats: ".history-stats-pane",
    board: ".analysis-board__board-frame",
    col: ".analysis-board__board-col",
  });

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
