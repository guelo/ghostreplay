import { expect, type APIRequestContext, type Page } from "@playwright/test";
import { test } from "./fixtures/auth";

type BlunderListItem = {
  id: number;
  bad_move: string;
  pass_streak: number;
  last_reviewed_at: string | null;
  srs_priority: number;
};

const apiBaseURL = process.env.E2E_API_URL ?? "http://127.0.0.1:8010";

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
    .poll(async () => {
      return page.locator(".move-list-grid .move-button").count();
    })
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

const getToken = async (page: Page): Promise<string> => {
  const token = await page.evaluate(() =>
    localStorage.getItem("ghost_replay_token"),
  );
  if (!token) {
    throw new Error("Expected auth token in localStorage");
  }
  return token;
};

const fetchBlunders = async (
  request: APIRequestContext,
  token: string,
): Promise<BlunderListItem[]> => {
  const response = await request.get(`${apiBaseURL}/api/blunder`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.ok()).toBeTruthy();
  return response.json() as Promise<BlunderListItem[]>;
};

const waitForBlunderState = async (
  page: Page,
  predicate: (item: BlunderListItem) => boolean,
): Promise<BlunderListItem> => {
  const token = await getToken(page);
  const deadline = Date.now() + 40_000;

  while (Date.now() < deadline) {
    const blunders = await fetchBlunders(page.request, token);
    const matched = blunders.find((item) => predicate(item));
    if (matched) {
      return matched;
    }
    await page.waitForTimeout(250);
  }

  throw new Error("Timed out waiting for expected blunder state");
};

const startNewGameAsWhite = async (page: Page): Promise<void> => {
  await page
    .locator(".game-end-banner")
    .getByRole("button", { name: /new game/i })
    .click();
  const playWhiteButton = page.getByRole("button", { name: /play white/i });
  await expect(playWhiteButton).toBeVisible();
  await playWhiteButton.click();

  // Some local branches gate game start behind a secondary "Play" submit button.
  const playButton = page.getByRole("button", { name: /^play$/i });
  if (
    (await playButton.count()) > 0 &&
    (await playButton.first().isVisible())
  ) {
    await playButton.first().click();
  }

  await expect
    .poll(
      async () => {
        return page
          .locator(".chess-meta")
          .filter({ hasText: "Session:" })
          .first()
          .textContent();
      },
      { timeout: 15_000 },
    )
    .toContain("Active");
};

const resignCurrentGame = async (page: Page): Promise<void> => {
  await page.getByRole("button", { name: "Resign" }).click();
  await expect(page.locator(".game-end-banner-message")).toContainText(
    "You resigned.",
  );
};

const playToSeededReviewPosition = async (page: Page): Promise<void> => {
  await playMove(page, "e2", "e4");
  await waitForMoveCountAtLeast(page, 2);
  await playMove(page, "g1", "f3");
  await waitForMoveCountAtLeast(page, 4);
  await playMove(page, "f1", "c4");
  await waitForMoveCountAtLeast(page, 6);
  await expect(page.getByText("Review Position")).toBeVisible();
};

test("seeded due blunder flow: game -> ghost review fail -> ghost review pass -> SRS updates", async ({
  page,
  loginAs,
}) => {
  test.setTimeout(120_000);

  await loginAs(page, "due");
  await page.goto("/blunders");
  await expect(
    page.getByRole("heading", { name: "Blunder Library" }),
  ).toBeVisible();
  await expect(
    page.locator(".blunder-card").filter({ hasText: "Nxe5" }).first(),
  ).toBeVisible();

  const seededTarget = await waitForBlunderState(
    page,
    (item) => item.bad_move === "Nxe5",
  );
  expect(seededTarget.last_reviewed_at).not.toBeNull();
  const initialReviewedAt = seededTarget.last_reviewed_at;

  await page.goto("/game");

  // Game 1: replay the known blunder in the review position (fail path).
  await startNewGameAsWhite(page);
  await playToSeededReviewPosition(page);
  await expect(page.locator(".ghost-mode-label")).toHaveText("Replay Ghost");
  await playMove(page, "e1", "e2");
  const continueButton = page.getByRole("button", { name: "Continue" });
  if (
    (await continueButton.count()) > 0 &&
    (await continueButton.first().isVisible())
  ) {
    await continueButton.first().click();
  }

  const afterFail = await waitForBlunderState(
    page,
    (item) =>
      item.id === seededTarget.id &&
      item.pass_streak === 0 &&
      item.last_reviewed_at !== null &&
      item.last_reviewed_at !== initialReviewedAt,
  );
  const failReviewedAt = afterFail.last_reviewed_at;
  expect(failReviewedAt).not.toBeNull();

  await resignCurrentGame(page);

  // Game 2: choose the safer move from the same review position (pass path).
  await startNewGameAsWhite(page);
  await playToSeededReviewPosition(page);
  await playMove(page, "c2", "c3");

  const afterPass = await waitForBlunderState(
    page,
    (item) =>
      item.id === seededTarget.id &&
      item.pass_streak === 1 &&
      item.last_reviewed_at !== null &&
      item.last_reviewed_at !== failReviewedAt,
  );
  expect(afterPass.srs_priority).toBeLessThanOrEqual(1);

  await page.goto("/blunders");
  await expect(
    page.getByRole("heading", { name: "Blunder Library" }),
  ).toBeVisible();
  await page
    .locator(".blunder-card")
    .filter({ hasText: "Nxe5" })
    .first()
    .click();
  await expect(
    page
      .locator(".blunder-detail__stat")
      .filter({ hasText: "Pass streak" })
      .locator(".blunder-detail__stat-value"),
  ).toHaveText("1");
});
