import { expect, type Page } from "@playwright/test";
import { apiURL } from "../env";
import { test } from "../fixtures/auth";
import { waitForMoveCountAtLeast as waitForCount } from "../fixtures/moves";
import {
  buildIndex,
  captureState,
  failRoute,
  FIXED_TIME,
  prepareDeterministicPage,
  stallRoute,
  viewportsFor,
} from "./helpers";

/**
 * Screenshot gallery suite (g-tsyy). Captures a FIXED first-pass inventory of UI
 * states per page across the app's real breakpoints. Output is a reviewable
 * contact sheet (output/index.html), NOT pixel-diff assertions.
 *
 * Runs SERIAL because playwright.config has fullyParallel:true — parallel
 * workers would race on the shared output dir and index generation.
 */
test.describe.configure({ mode: "serial", timeout: 180_000 });

/**
 * Scrub an analysis board to its final ply so both material displays are filled.
 *
 * /game and /history open the board at the game's FIRST move (initialMoveIndex=0),
 * where nothing has been captured. The seeded stable game ends with White up a
 * knight + pawn and Black up a bishop (see backend/scripts/seed_e2e_data.py) —
 * MaterialDisplay renders per-type NET surpluses, so both sides only draw icons
 * once they are ahead in different piece types. The selector covers both list
 * layouts (MoveList's `.move-button`, narrow HorizontalMoveList's `.h-move`);
 * the chosen index then survives the capture loop's resizes because the
 * AnalysisBoard instance (and its currentIndex state) outlives the list swap.
 */
const showCapturedMaterial = async (page: Page): Promise<void> => {
  await page.locator(".move-list-grid .move-button, .h-move").last().click();
  await expect(
    page.locator(".analysis-board__material--player .material-icons"),
  ).toBeVisible();
  await expect(
    page.locator(".analysis-board__material--opponent .material-icons"),
  ).toBeVisible();
};

/** Capture one state across every viewport that page cares about. */
const captureAcrossViewports = async (
  page: Page,
  testInfo: import("@playwright/test").TestInfo,
  opts: {
    pageKey: string;
    state: string;
    waitFor?: (page: Page) => import("@playwright/test").Locator;
    fullPage?: boolean;
  },
): Promise<void> => {
  for (const viewport of viewportsFor(opts.pageKey)) {
    await captureState(page, testInfo, {
      page: opts.pageKey,
      state: opts.state,
      viewport,
      waitFor: opts.waitFor?.(page),
      fullPage: opts.fullPage,
    });
  }
};

test.afterAll(() => {
  buildIndex();
});

// --- Landing -------------------------------------------------------------

test.describe("landing", () => {
  test("anonymous + authed", async ({ page, loginAs }) => {
    await prepareDeterministicPage(page);
    await page.goto("/");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "landing",
      state: "anonymous",
      waitFor: (p) => p.locator(".nav-bar"),
    });

    await loginAs(page, "stable");
    await page.goto("/");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "landing",
      state: "authed",
      waitFor: (p) => p.locator(".nav-bar"),
    });
  });
});

// --- Auth ----------------------------------------------------------------

test.describe("login", () => {
  test("empty + validation error", async ({ page }) => {
    await prepareDeterministicPage(page);
    await page.goto("/login");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "login",
      state: "empty",
      waitFor: (p) => p.locator(".auth-form"),
    });

    // Submitting an empty form trips client-side validation.
    await page.getByRole("button", { name: "Log in" }).click();
    await captureAcrossViewports(page, test.info(), {
      pageKey: "login",
      state: "validation-error",
      waitFor: (p) => p.locator(".auth-form__error"),
    });
  });
});

test.describe("register", () => {
  test("empty + password mismatch", async ({ page }) => {
    await prepareDeterministicPage(page);
    await page.goto("/register");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "register",
      state: "empty",
      waitFor: (p) => p.locator(".auth-form"),
    });

    const inputs = page.locator(".auth-form__input");
    await inputs.nth(0).fill("new_player_e2e");
    await inputs.nth(1).fill("password-one");
    await inputs.nth(2).fill("password-two");
    await page.getByRole("button", { name: "Register" }).click();
    await expect(page.locator(".auth-form__error")).toContainText(
      "Passwords do not match",
    );
    await captureAcrossViewports(page, test.info(), {
      pageKey: "register",
      state: "password-mismatch",
      waitFor: (p) => p.locator(".auth-form__error"),
    });
  });
});

// --- History -------------------------------------------------------------

test.describe("history", () => {
  test("loading / empty / populated / error", async ({ page, loginAs }) => {
    // Loading: stall the history fetch so the placeholder persists.
    await prepareDeterministicPage(page);
    await loginAs(page, "stable");
    await stallRoute(page, "**/api/history**");
    await page.goto("/history");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "history",
      state: "loading",
      waitFor: (p) => p.locator(".history-shell"),
    });
    await page.unrouteAll();

    // Empty: the empty seed user has no games.
    await loginAs(page, "empty");
    await page.goto("/history");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "history",
      state: "empty",
      waitFor: (p) => p.getByText("No games played yet"),
    });

    // Populated + selected analysis: the stable user has games and the page
    // auto-selects the first one, so wait for the analysis board to render, then
    // scrub to the final ply so the captured-material rows are populated.
    await loginAs(page, "stable");
    await page.goto("/history");
    await expect(page.locator(".analysis-board")).toBeVisible();
    await showCapturedMaterial(page);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "history",
      state: "populated",
      waitFor: (p) => p.locator(".analysis-board"),
    });

    // Error: force a 500 on the history fetch.
    await failRoute(page, "**/api/history**");
    await page.goto("/history");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "history",
      state: "error",
      waitFor: (p) => p.locator(".history-shell__error"),
    });
    await page.unrouteAll();
  });
});

// --- Openings ------------------------------------------------------------

test.describe("openings", () => {
  test("side-selection / loading / populated / selected-line / no-data / error", async ({
    page,
    loginAs,
  }) => {
    // The opening graph is a process-wide singleton with no build lock, so the
    // first cold request can take ~30s+ (longer under concurrent rebuilds).
    test.setTimeout(300_000);
    await prepareDeterministicPage(page);

    // Side selection: the bare entry point intentionally mounts no explorer and
    // issues no tree request until the player explicitly chooses a color.
    await loginAs(page, "due");
    await page.goto("/openings");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "openings",
      state: "side-selection",
      waitFor: (p) => p.locator(".openings-selection-gate"),
    });

    // Loading: stall the tree fetch, then enter the tree through the real color
    // toggle so the skeleton persists after the gate-to-explorer transition.
    await stallRoute(page, "**/api/openings/tree**");
    await page.getByRole("button", { name: "White" }).click();
    await expect(page).toHaveURL(/[?&]color=white(?:&|$)/);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "openings",
      state: "loading",
      waitFor: (p) => p.locator(".openings-state--loading"),
    });
    await page.unrouteAll();

    // Populated: the due user has games → a scored tree workspace renders. The
    // first real request warms the singleton graph; allow a wide window.
    await page.goto("/openings?color=white");
    await expect(page.locator(".openings-state--loading")).toBeHidden({
      timeout: 180_000,
    });
    await captureAcrossViewports(page, test.info(), {
      pageKey: "openings",
      state: "populated",
      waitFor: (p) => p.locator(".openings-tree-workspace"),
    });

    // Selected line: click a compact node card to expand a deeper column and
    // draw the selected-path connectors. captureAcrossViewports only waits for
    // the locator it is handed before each shot, so gate on the SETTLED selected
    // state here before the capture loop, or it could catch a provisional frame.
    // Crucially, the root state ALREADY has an expanded card (the "Starting
    // position" root card), no append loading, and ≥1 connector — so first wait
    // for SELECTION-specific evidence (URL gains move=, and the expanded card
    // gains the non-root move-list line that the starting-position card lacks),
    // then for the deeper column to settle (append loading gone + a connector
    // drawn). The card header is the opening name, so its copy does not identify
    // whether the selected node has replaced the root.
    await page.locator("button.tree-node-card--compact").first().click();
    await page.waitForURL(/[?&]move=/);
    await expect(
      page.locator(".tree-node-card--expanded .tree-node-card__move-list"),
    ).toBeVisible();
    await expect(page.locator(".openings-tree-append--loading")).toHaveCount(0);
    await expect
      .poll(() => page.locator("path.openings-tree-connector").count())
      .toBeGreaterThan(0);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "openings",
      state: "selected-line",
      waitFor: (p) => p.locator(".tree-node-card--expanded"),
    });

    // No-data: the empty user has no games → book-only tree + banner.
    await loginAs(page, "empty");
    await page.goto("/openings?color=white");
    await expect(page.locator(".openings-state--loading")).toBeHidden({
      timeout: 60_000,
    });
    await captureAcrossViewports(page, test.info(), {
      pageKey: "openings",
      state: "no-data",
      waitFor: (p) => p.locator(".openings-tree__nodata-banner"),
    });

    // Error: force a failure on the tree fetch.
    await failRoute(page, "**/api/openings/tree**");
    await page.goto("/openings?color=white");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "openings",
      state: "error",
      waitFor: (p) => p.locator(".openings-state--error"),
    });
    await page.unrouteAll();
  });
});

// --- Stats ---------------------------------------------------------------

test.describe("stats", () => {
  test("loading / empty / populated / error", async ({ page, loginAs }) => {
    await prepareDeterministicPage(page);
    await loginAs(page, "due");
    // Stall BOTH summary and rating history so the page stays in loading.
    await stallRoute(page, "**/api/stats/summary**");
    await stallRoute(page, "**/api/stats/rating-history**");
    await page.goto("/stats");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "stats",
      state: "loading",
      waitFor: (p) => p.locator(".stats-shell__placeholder").first(),
    });
    await page.unrouteAll();

    await loginAs(page, "empty");
    await page.goto("/stats");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "stats",
      state: "empty",
      waitFor: (p) => p.locator(".stats-shell__empty"),
    });

    await loginAs(page, "due");
    await page.goto("/stats");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "stats",
      state: "populated",
      waitFor: (p) => p.locator(".stats-section").first(),
    });

    // Error: fail BOTH endpoints so summary + graph both show error.
    await failRoute(page, "**/api/stats/summary**");
    await failRoute(page, "**/api/stats/rating-history**");
    await page.goto("/stats");
    await captureAcrossViewports(page, test.info(), {
      pageKey: "stats",
      state: "error",
      waitFor: (p) => p.locator(".stats-shell__error"),
    });
    await page.unrouteAll();
  });
});

// --- Game analysis -------------------------------------------------------

test.describe("game", () => {
  test("loading / processing / populated / errors", async ({
    page,
    loginAs,
  }) => {
    await prepareDeterministicPage(page);
    await loginAs(page, "stable");

    // Resolve a real session id from history to drive the populated state.
    // loginAs seeds the token via addInitScript, so navigate once to apply it,
    // then read it back and query the API directly. The history response shape
    // is { games: [{ session_id }] } (see api.ts fetchHistory).
    await page.goto("/history");
    const token = await page.evaluate(() =>
      localStorage.getItem("ghost_replay_token"),
    );
    const historyRes = await page.request.get(
      `${apiURL}/api/history?limit=1`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    const historyData = (await historyRes.json()) as {
      games?: { session_id?: string }[];
    };
    const sessionId = historyData?.games?.[0]?.session_id ?? null;
    expect(sessionId, "seeded stable user must have a game in history").toBeTruthy();
    const id = sessionId as string;

    // Loading: stall the analysis fetch.
    await stallRoute(page, "**/api/session/*/analysis**");
    await page.goto(`/game?id=${id}`);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "game",
      state: "loading",
      waitFor: (p) => p.locator(".history-shell__placeholder"),
    });
    await page.unrouteAll();

    // Populated: real analysis renders the board. Scrub to the final ply so the
    // captured-material rows are populated for both sides.
    await page.goto(`/game?id=${id}`);
    await expect(page.locator(".analysis-board")).toBeVisible();
    await showCapturedMaterial(page);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "game",
      state: "populated",
      waitFor: (p) => p.locator(".analysis-board"),
    });

    // Processing: analysis still computing (is_complete false).
    await mockAnalysis(page, { is_complete: false, analyzed_moves: 2 });
    await page.goto(`/game?id=${id}`);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "game",
      state: "processing",
      waitFor: (p) => p.locator(".analysis-pane__processing"),
    });
    await page.unrouteAll();

    // Missing-color error: analysis response without a player color.
    await mockAnalysis(page, { player_color: null });
    await page.goto(`/game?id=${id}`);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "game",
      state: "missing-color-error",
      waitFor: (p) => p.getByText(/missing player color/i),
    });
    await page.unrouteAll();

    // Terminal (non-retryable) error: GameAnalysisPage polls on retryable
    // errors, so a 404 is required to hit the terminal error branch.
    await failRoute(page, "**/api/session/*/analysis**", 404, {
      detail: "Not found",
    });
    await page.goto(`/game?id=${id}`);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "game",
      state: "terminal-error",
      waitFor: (p) => p.locator(".history-shell__error"),
    });
    await page.unrouteAll();
  });
});

/** Fulfill the session analysis endpoint with a synthetic SessionAnalysis. */
const mockAnalysis = (
  page: Page,
  overrides: Record<string, unknown>,
): Promise<void> =>
  page.route("**/api/session/*/analysis**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        session_id: "mock",
        pgn: "1. e4 e5",
        result: "1-0",
        moves: [
          {
            move_number: 1,
            color: "white",
            move_san: "e4",
            fen_after:
              "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            eval_cp: 20,
            eval_mate: null,
            best_move_san: "e4",
            best_move_eval_cp: 20,
            eval_delta: 0,
            classification: "best",
          },
        ],
        summary: {
          total_moves: 1,
          blunders: 0,
          mistakes: 0,
          inaccuracies: 0,
          average_centipawn_loss: 0,
          accuracy: 100,
        },
        position_analysis: {},
        expected_total_moves: 2,
        analyzed_moves: 1,
        is_complete: true,
        player_color: "white",
        ...overrides,
      }),
    }),
  );

// --- Play (live board) ---------------------------------------------------

const boardSquare = (page: Page, square: string) =>
  page
    .locator(".chessboard-board-area")
    .locator(`[data-square="${square}"]`)
    .first();

const playMove = async (page: Page, from: string, to: string): Promise<void> => {
  await boardSquare(page, from).click();
  await boardSquare(page, to).click();
};

const waitForMoveCountAtLeast = async (
  page: Page,
  minimum: number,
): Promise<void> => {
  // Counts half-moves across both layouts: the vertical list renders
  // `.move-list-grid .move-button`, while the narrow/mobile layout swaps in
  // HorizontalMoveList, whose half-moves are `.h-move` tokens.
  await waitForCount(
    page.locator(".move-list-grid .move-button, .h-move"),
    minimum,
  );
};

const startGameAsWhite = async (page: Page): Promise<void> => {
  // "Play White" starts the game immediately. (Note: a bare /^play$/i would
  // also match the Play/Drill mode toggle, which is the already-active default
  // and goes disabled the instant the game starts — never click that here.)
  const playWhite = page.getByRole("button", { name: /play white/i });
  // Wait for the button before clicking: the start overlay mounts after the
  // /play page resolves its data, so a bare `.count()` (which does NOT
  // auto-wait) can read 0 and silently skip the click, leaving the game
  // unstarted. `.click()` auto-waits, but assert visibility first for a clear
  // failure if the overlay never appears.
  await expect(playWhite).toBeVisible({ timeout: 15_000 });
  await playWhite.click();
  await expect(page.locator(".game-status-badge--live")).toBeVisible({
    timeout: 15_000,
  });
};

/**
 * Script the seeded Giuoco Piano review line used by the gallery's transient
 * review states.
 *
 * The due account is intentionally shared by the E2E suite, so another test
 * can refresh its SRS opportunity and make the real ghost selector fall back
 * to Maia. Routing the complete line keeps these visual-review artifacts
 * independent of mutable backend review state; the integration suite covers
 * the real selector and review write path.
 */
const routeSeededReviewGhostLine = async (page: Page): Promise<void> => {
  const ghostReplies: {
    move: { uci: string; san: string };
    target_blunder_id?: number;
    target_fen?: string;
    target_blunder_srs?: {
      last_reviewed_at: string | null;
      created_at: string | null;
      pass_count: number;
      fail_count: number;
      pass_streak: number;
    };
  }[] = [
    { move: { uci: "e7e5", san: "e5" } }, // reply to e4
    { move: { uci: "b8c6", san: "Nc6" } }, // reply to Nf3
    {
      // reply to Bc4 — arms the SRS review at the Giuoco Piano position
      move: { uci: "f8c5", san: "Bc5" },
      target_blunder_id: 1,
      // This must be the exact FEN AFTER Bc5 (white to move):
      // applyPlayerMove gates the SRS review via hasReviewTargetAtFen, whose
      // normalize_fen comparison keeps fields 1-4.
      target_fen:
        "r1bqk1nr/pppp1ppp/2n5/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
      target_blunder_srs: {
        last_reviewed_at: "2026-05-01T12:00:00Z",
        created_at: "2026-04-01T12:00:00Z",
        pass_count: 0,
        fail_count: 1,
        pass_streak: 0,
      },
    },
    { move: { uci: "g8f6", san: "Nf6" } }, // reply to the failing Ke2
  ];
  let ghostReplyIndex = 0;

  await page.route("**/api/game/next-opponent-move", (route) => {
    const reply =
      ghostReplies[Math.min(ghostReplyIndex, ghostReplies.length - 1)];
    ghostReplyIndex += 1;
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        // Match replay mode throughout; only the first reply transitions the
        // live game from engine to ghost.
        mode: "ghost",
        move: reply.move,
        target_blunder_id: reply.target_blunder_id ?? null,
        target_blunder_srs: reply.target_blunder_srs ?? null,
        target_fen: reply.target_fen ?? null,
        decision_source: "ghost_path",
        drill_route: null,
      }),
    });
  });
};

/**
 * Tick the (paused) clock frame-by-frame until the SRS-fail spotlight content
 * re-anchors to the CURRENT board rect. The scrim re-measures on resize via a
 * rAF the paused clock won't fire on its own, so we poll the measured geometry
 * (content's inline left/top vs the board's bounding box) rather than trusting
 * a fixed delay. SrsFailSpotlight sets left = rect.cx, top = rect.top + min(18,
 * height*0.04); matching those means the new measurement has committed.
 */
const settleSpotlightGeometry = async (page: Page): Promise<void> => {
  await expect
    .poll(
      async () => {
        await page.clock.runFor(64);
        return page.evaluate(() => {
          const content =
            document.querySelector<HTMLElement>(".srs-fail-content");
          const board = document.querySelector<HTMLElement>(
            ".chessboard-square-measure",
          );
          if (!content || !board) return Number.POSITIVE_INFINITY;
          const b = board.getBoundingClientRect();
          const left = parseFloat(content.style.left || "0");
          const top = parseFloat(content.style.top || "0");
          const expectedLeft = b.left + b.width / 2;
          const expectedTop = b.top + Math.min(18, b.height * 0.04);
          return Math.max(
            Math.abs(left - expectedLeft),
            Math.abs(top - expectedTop),
          );
        });
      },
      { timeout: 5_000 },
    )
    .toBeLessThan(1.5);
};

test.describe("play", () => {
  test("fresh board + review toast", async ({ page, loginAs }) => {
    await prepareDeterministicPage(page);
    await loginAs(page, "due");
    await routeSeededReviewGhostLine(page);
    await page.goto("/play");

    // Fresh board: start a new game as White.
    await startGameAsWhite(page);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "play",
      state: "fresh-board",
      waitFor: (p) => p.locator(".chessboard-board-area"),
    });

    // Mid-game live + seeded review-warning toast (reuses the seeded review path
    // from mobile-game-layout.spec.ts).
    await page.setViewportSize({ width: 390, height: 844 });
    await playMove(page, "e2", "e4");
    await waitForMoveCountAtLeast(page, 2);
    await playMove(page, "g1", "f3");
    await waitForMoveCountAtLeast(page, 4);
    await playMove(page, "f1", "c4");
    await waitForMoveCountAtLeast(page, 6);
    await expect(
      page.locator(".board-notice--review-warning:visible"),
    ).toBeVisible({ timeout: 30_000 });

    // The review warning auto-dismisses after four seconds. Freeze it only
    // after it has appeared so six sequential viewport captures cannot race
    // its timer under full-suite load.
    await page.clock.pauseAt(FIXED_TIME);
    await captureAcrossViewports(page, test.info(), {
      pageKey: "play",
      state: "review-warning-toast",
      // The review warning renders as a single on-board notice (top-left).
      waitFor: (p) => p.locator(".board-notice--review-warning:visible"),
    });
    await page.unrouteAll();
  });

  test("mid-game (no toasts)", async ({ page, loginAs }) => {
    // Captured in its own game so clearing the "Ghost reactivated" rehook toast
    // (which ends ghost steering) can't interfere with the review-toast flow.
    await prepareDeterministicPage(page);
    await loginAs(page, "due");

    // Script the opponent so the mid-game board has captured pieces on BOTH
    // sides. The due user's REAL ghost line (e4 e5 Nf3 Nc6 Bc4 Bc5 Nxe5) reaches
    // its first capture only at the seeded blunder, which arms the SRS review
    // warning and then the fail spotlight — toasts this state must not have. So
    // deviate into the Ruy Lopez Exchange (3.Bb5 a6 4.Bxc6 dxc6 5.Nxe5), where
    // White nets a knight + pawn and Black nets a bishop: MaterialDisplay draws
    // per-type NET surpluses, so each side needs a lead in a DIFFERENT piece to
    // render an icon (an even swap of the same piece cancels out and draws
    // nothing). mode "ghost" keeps the opponent badge the state had before;
    // target_fen null means no review is ever armed off this line.
    const ghostReplies = [
      { uci: "e7e5", san: "e5" }, // reply to e4
      { uci: "b8c6", san: "Nc6" }, // reply to Nf3
      { uci: "a7a6", san: "a6" }, // reply to Bb5
      { uci: "d7c6", san: "dxc6" }, // recaptures the bishop on c6
      { uci: "d8d4", san: "Qd4" }, // reply to Nxe5 (no capture back)
    ];
    let ghostReplyIndex = 0;
    await page.route("**/api/game/next-opponent-move", (route) => {
      const reply =
        ghostReplies[Math.min(ghostReplyIndex, ghostReplies.length - 1)];
      ghostReplyIndex += 1;
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          mode: "ghost",
          move: reply,
          target_blunder_id: null,
          target_blunder_srs: null,
          target_fen: null,
          decision_source: "ghost_path",
          drill_route: null,
        }),
      });
    });

    await page.goto("/play");
    await startGameAsWhite(page);

    await page.setViewportSize({ width: 390, height: 844 });
    await playMove(page, "e2", "e4");
    await waitForMoveCountAtLeast(page, 2);
    await playMove(page, "g1", "f3");
    await waitForMoveCountAtLeast(page, 4);
    await playMove(page, "f1", "b5");
    await waitForMoveCountAtLeast(page, 6);
    await playMove(page, "b5", "c6"); // Bxc6 — White wins the knight
    await waitForMoveCountAtLeast(page, 8);
    await playMove(page, "f3", "e5"); // Nxe5 — and the e5 pawn
    await waitForMoveCountAtLeast(page, 10);

    // The rehook board notice auto-dismisses after 3s (useBoardNotice) but the
    // frozen clock blocks that timer — advance it so the board is notice-free.
    await page.clock.runFor(3500);
    await expect(page.locator(".board-notice:visible")).toHaveCount(0);

    // Both sides show captures: exactly two material rows are visible in either
    // layout (portrait relocates them into the info panel + controls row and
    // hides the moves-column originals), and MaterialDisplay only renders
    // .material-icons for a side that is actually ahead in some piece type.
    await expect(
      page.locator(".material-display .material-icons:visible"),
    ).toHaveCount(2);

    await captureAcrossViewports(page, test.info(), {
      pageKey: "play",
      state: "mid-game",
      waitFor: (p) =>
        p.locator(".move-list-grid .move-button, .h-move").first(),
    });
    await page.unrouteAll();
  });

  // --- Game-end banner (resign path) ------------------------------------
  // Resigning reaches the banner deterministically (no long game needed). The
  // rating delta comes from POST /api/game/end, which is non-deterministic, so
  // mock it with a fixed EndGameResponse for a stable banner.
  test("game-end banner", async ({ page, loginAs }) => {
    await prepareDeterministicPage(page);
    await loginAs(page, "due");

    const endGamePayload = {
      session_id: "e2e-resign",
      result: "resign",
      ended_at: "2026-06-01T12:00:00Z",
      rating: {
        rating_before: 1200,
        rating_after: 1188,
        is_provisional: false,
      },
      // The banner renders score_changes.elo.rating as the DELTA, so use -12 to
      // match the 1200 -> 1188 rating_before/after for a realistic banner.
      score_changes: {
        elo: { rating: -12, is_provisional: false },
        chesscom: null,
        lichess: null,
      },
    };
    await page.route("**/api/game/end", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(endGamePayload),
      }),
    );

    await page.goto("/play");
    await startGameAsWhite(page);

    // Play one move so resign is enabled, then open the resign warning dialog.
    await playMove(page, "e2", "e4");
    await waitForMoveCountAtLeast(page, 2);
    await page.getByRole("button", { name: "Resign" }).click();
    // Both the board control and the dialog button are named "Resign"; scope
    // the confirm click through the alertdialog to avoid a strict-mode match.
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Resign" })
      .click();

    await captureAcrossViewports(page, test.info(), {
      pageKey: "play",
      state: "game-end-banner",
      waitFor: (p) => p.locator(".game-end-banner-message"),
    });
    await page.unrouteAll();
  });

  // --- Drill setup overlay (deterministic, no mocks) --------------------
  test("drill setup overlay", async ({ page, loginAs }) => {
    // The openings graph is a process-wide singleton; first cold load is slow.
    test.setTimeout(300_000);
    await prepareDeterministicPage(page);
    await loginAs(page, "due");
    await page.goto("/play");

    // The start overlay shows on load (showStartOverlay defaults true). Switch
    // to drill mode via the "Drill" toggle in the overlay.
    await page.getByRole("button", { name: "Drill" }).click();

    // DrillSetupPanel is a fragment (no root) and .opening-picker renders while
    // openings still load, so gate on the trigger label flipping to its loaded
    // state instead of a panel root.
    await expect(
      page.locator(".opening-picker__trigger"),
    ).toHaveText(/Select opening/, { timeout: 180_000 });

    // Pick a strictness tier — the fine-tune slider only renders once
    // strictnessCp is non-null, so without this the panel captures without it.
    await page
      .locator(".strictness-tier-grid")
      .getByRole("button", { name: "Standard" })
      .click();
    await expect(page.locator(".strictness-slider-row")).toBeVisible();

    await captureAcrossViewports(page, test.info(), {
      pageKey: "play",
      state: "drill-setup",
      waitFor: (p) => p.locator(".strictness-slider-row"),
    });
  });

  // --- Review-fail spotlight (real player moves, scripted replies) ------
  // Replay the seeded SRS review and FAIL it (Ke2) to fire the full-screen
  // spotlight scrim. The scrim auto-dismisses on a timer and re-measures its
  // clip-path hole via rAF, so pause the clock and tick one frame per resize.
  test("review-fail spotlight", async ({ page, loginAs }) => {
    test.setTimeout(120_000);
    await prepareDeterministicPage(page);
    await loginAs(page, "due");

    // Mock the COMPLETE opponent reply sequence before any move. The backend
    // ghost path is timing-sensitive: on a cold/fresh DB a reply can fall
    // through to Maia (e.g. Nf6 instead of Bc5), producing the wrong opening
    // (C55) and arming no review. Scripting every reply makes the whole line
    // deterministic.
    await routeSeededReviewGhostLine(page);

    await page.goto("/play");
    await startGameAsWhite(page);

    // Reach the seeded review position (blunder Nxe5) via the scripted ghost
    // line. Play at the default viewport; the capture loop resizes per viewport.
    await playMove(page, "e2", "e4");
    await waitForMoveCountAtLeast(page, 2);
    await playMove(page, "g1", "f3");
    await waitForMoveCountAtLeast(page, 4);
    await playMove(page, "f1", "c4");
    await waitForMoveCountAtLeast(page, 6);
    await expect(
      page.locator(".board-notice--review-warning:visible"),
    ).toBeVisible({
      timeout: 30_000,
    });

    // The spotlight is the state under test. Require the live opening-lineage
    // panel to have loaded, but do not couple this visual test to the deepest
    // family returned by the eventually consistent move-upload path: gameplay
    // intentionally uses a bounded re-poll and this scripted six-ply sequence
    // runs much faster than a human game.
    await expect(page.locator(".game-opening-lineage")).toBeVisible({
      timeout: 15_000,
    });

    // The first ghost reply (engine->ghost) raised the "The haunting resumes"
    // rehook notice; reaching the review position then preempted it with the
    // review warning (single board-notice slot). Advance the frozen clock to
    // clear any lingering rehook timer and confirm no rehook is in the capture.
    await page.clock.runFor(3500);
    await expect(page.locator(".board-notice--rehook:visible")).toHaveCount(0);

    // Play the recorded fail move (king move) to trigger the spotlight. The
    // clock MUST stay running across this move: the coordinator dispatches the
    // move's analysis through a trailing cache-lookup debounce (a main-thread
    // setTimeout, GameAnalysisCoordinator CACHE_LOOKUP_DEBOUNCE_MS). A paused
    // clock freezes that timer, so the analysis — and therefore the SRS
    // pass/fail grade that arms the spotlight — would never resolve. Pause only
    // AFTER the scrim is up, to freeze its hold/shrink timers for the capture.
    await playMove(page, "e1", "e2");
    await expect(page.locator(".srs-fail-scrim")).toBeVisible({
      timeout: 15_000,
    });

    // Pause the clock so the spotlight's hold/shrink timers don't advance and
    // dismiss the scrim mid-capture.
    await page.clock.pauseAt(FIXED_TIME);

    // Dedicated capture loop. The scrim's clip-path hole + headline position are
    // set from a board getBoundingClientRect() that re-measures on resize via a
    // rAF the PAUSED clock won't auto-fire. After each resize, tick frames until
    // the spotlight content actually re-anchors to the new board rect (polling
    // the measured geometry, not a fixed delay), then screenshot directly so a
    // second captureState resize can't re-stale the geometry.
    for (const viewport of viewportsFor("play")) {
      await page.setViewportSize({
        width: viewport.width,
        height: viewport.height,
      });
      await settleSpotlightGeometry(page);
      await captureState(page, test.info(), {
        page: "play",
        state: "review-fail-spotlight",
        viewport,
        waitFor: page.locator(".srs-fail-scrim"),
        // The geometry is already settled at this viewport; a captureState
        // resize would fire another resize -> rAF and re-stale it mid-capture.
        skipResize: true,
      });
    }
    await page.unrouteAll();
  });

  // --- Promotion picker (scripted opponent line) ------------------------
  // The picker fires only when the LOCAL board has a pawn one step from the
  // last rank, so script an exact legal line by mocking the single opponent
  // endpoint. White escorts the a-pawn to b7 then captures b7xc8; Black just
  // shuffles the g8 knight (never touching the advancing pawn).
  test("promotion picker", async ({ page, loginAs }) => {
    await prepareDeterministicPage(page);
    await loginAs(page, "due");

    // Alternating legal Black knight replies (g8<->f6). Each reply is the FULL
    // NextOpponentMoveResponse contract; mode stays "engine" so no ghost-rehook
    // toast (engine->ghost transition) pollutes the screenshot.
    const blackReplies = [
      { uci: "g8f6", san: "Nf6" },
      { uci: "f6g8", san: "Ng8" },
      { uci: "g8f6", san: "Nf6" },
      { uci: "f6g8", san: "Ng8" },
    ];
    let replyIndex = 0;
    await page.route("**/api/game/next-opponent-move", (route) => {
      const move = blackReplies[Math.min(replyIndex, blackReplies.length - 1)];
      replyIndex += 1;
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          mode: "engine",
          move,
          target_blunder_id: null,
          target_blunder_srs: null,
          target_fen: null,
          decision_source: "backend_engine",
          drill_route: null,
        }),
      });
    });

    await page.goto("/play");
    await startGameAsWhite(page);

    // Escort the a-pawn: a4, a5, a6, axb7 (each followed by a knight reply),
    // then b7xc8 reaches the last rank with no promotion piece -> picker.
    await playMove(page, "a2", "a4");
    await waitForMoveCountAtLeast(page, 2);
    await playMove(page, "a4", "a5");
    await waitForMoveCountAtLeast(page, 4);
    await playMove(page, "a5", "a6");
    await waitForMoveCountAtLeast(page, 6);
    await playMove(page, "a6", "b7");
    await waitForMoveCountAtLeast(page, 8);
    await playMove(page, "b7", "c8");

    await captureAcrossViewports(page, test.info(), {
      pageKey: "play",
      state: "promotion-picker",
      waitFor: (p) => p.locator(".promotion-picker-square").first(),
    });
    await page.unrouteAll();
  });
});
