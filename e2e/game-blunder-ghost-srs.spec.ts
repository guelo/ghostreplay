import { expect, type APIRequestContext, type Page } from "@playwright/test";
import { apiURL as apiBaseURL } from "./env";
import { test } from "./fixtures/auth";
import { waitForMoveCountAtLeast as waitForCount } from "./fixtures/moves";

type BlunderListItem = {
  id: number;
  bad_move: string;
  pass_streak: number;
  last_reviewed_at: string | null;
  srs_priority: number;
};

const boardSquare = (page: Page, square: string) =>
  page
    .locator(".chessboard-board-area")
    .locator(`[data-square="${square}"]`)
    .first();

const waitForMoveCountAtLeast = async (
  page: Page,
  minimum: number,
): Promise<void> => {
  await waitForCount(page.locator(".move-list-grid .move-button"), minimum);
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
  const data = (await response.json()) as { items: BlunderListItem[] };
  return data.items;
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
  // The setup overlay auto-opens on a fresh /game load (sessionId === null), but
  // after a game ends only the "New game" banner is shown and must be clicked to
  // reopen the overlay. Wait for the overlay; open it via the banner if absent.
  const playWhiteButton = page.getByRole("button", { name: /play white/i });
  try {
    await playWhiteButton.waitFor({ state: "visible", timeout: 3_000 });
  } catch {
    await page
      .locator(".game-end-banner")
      .getByRole("button", { name: /new game/i })
      .click();
    await playWhiteButton.waitFor({ state: "visible", timeout: 10_000 });
  }
  // Selecting a side in the StartPanel starts the game directly (no secondary
  // submit); the "Play" toggle tab tears down with the overlay as the game begins.
  await playWhiteButton.click();

  // The Resign button only renders for an active game — use it as the signal
  // that the session started and the overlay closed.
  await expect(page.getByRole("button", { name: "Resign" })).toBeVisible({
    timeout: 15_000,
  });
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

test("seeded due blunder flow: game -> ghost review fail -> SRS updates", async ({
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
  expect(afterFail.last_reviewed_at).not.toBeNull();

  // The Blunder Library reflects the failed review: pass streak stays at 0.
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
  ).toHaveText("0");
});
