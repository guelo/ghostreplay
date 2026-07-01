import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import StatsPage from "./StatsPage";

const mockLogout = vi.fn();
const getStatsSummaryMock = vi.fn();
const captureEventMock = vi.fn();

vi.mock("../analytics/posthog", () => ({
  captureEvent: (...args: unknown[]) => captureEventMock(...args),
}));

vi.mock("../contexts/useAuth", () => ({
  useAuth: () => ({
    user: {
      id: 1,
      username: "tester",
      isAnonymous: false,
    },
    logout: mockLogout,
  }),
}));

vi.mock("../utils/api", () => ({
  getStatsSummary: (...args: unknown[]) => getStatsSummaryMock(...args),
  fetchRatingHistory: vi.fn().mockResolvedValue({ ratings: [], current_rating: 1200, games_played: 0 }),
}));

const baseSummary = {
  window_days: 30,
  generated_at: "2026-02-01T00:00:00Z",
  games: {
    played: 12,
    score_pct: 58.3,
    wins: 5,
    losses: 3,
    draws: 2,
    avg_moves: 37.5,
  },
  moves: {
    accuracy_pct: 84.2,
    mistake_free_game_rate: 40.0,
    quality_distribution: {
      inaccuracy: 14.0,
      mistake: 9.0,
      blunder: 4.0,
    },
  },
  colors: {
    white: { games: 6, score_pct: 62.5, accuracy_pct: 85.0 },
    black: { games: 6, score_pct: 54.0, accuracy_pct: 83.0 },
  },
  training: {
    retention_pct: 66.7,
    reviewed_blunders: 9,
    retained_blunders: 6,
    review_pass_rate: 75.0,
    reviews_total: 20,
    reviews_passed: 15,
    conversions_in_window: 4,
    mastery_threshold: 3,
  },
  library: {
    blunders_total: 73,
    new_blunders_in_window: 9,
    avg_blunder_eval_loss_cp: 185,
    top_costly_blunders: [
      {
        blunder_id: 10,
        eval_loss_cp: 430,
        bad_move_san: "Qxh7+",
        best_move_san: "Re1",
        created_at: "2026-01-31T00:00:00Z",
      },
    ],
  },
  openings: {
    strongest: [
      {
        opening_name: "Ruy Lopez",
        opening_family: "Ruy Lopez",
        player_color: "white",
        opening_score: 55,
        sample_size: 10,
        game_count: 8,
      },
    ],
    weakest: [
      {
        opening_name: "Sicilian",
        opening_family: "Sicilian",
        player_color: "black",
        opening_score: 21,
        sample_size: 8,
        game_count: 6,
      },
    ],
  },
};

function renderPage() {
  return render(
    <MemoryRouter>
      <StatsPage />
    </MemoryRouter>,
  );
}

describe("StatsPage", () => {
  beforeEach(() => {
    getStatsSummaryMock.mockReset();
    mockLogout.mockReset();
    captureEventMock.mockReset();
  });

  it("loads and renders the reworked sections", async () => {
    getStatsSummaryMock.mockResolvedValueOnce(baseSummary);

    renderPage();

    expect(screen.getByText("Loading stats...")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("Results")).toBeInTheDocument();
    });

    expect(getStatsSummaryMock).toHaveBeenCalledWith(30);
    // New framing: rates over raw counts.
    expect(screen.getByText("58.3%")).toBeInTheDocument(); // Score %
    expect(screen.getByText("84.2%")).toBeInTheDocument(); // Accuracy %
    expect(screen.getByText("5–3–2 W–L–D")).toBeInTheDocument();
    // Training conversion.
    expect(screen.getByText("Blunders Mastered")).toBeInTheDocument();
    expect(screen.getByText("Reached 3-pass streak")).toBeInTheDocument();
    // Openings strongest / weakest.
    expect(screen.getByText("Ruy Lopez")).toBeInTheDocument();
    expect(screen.getByText("Sicilian")).toBeInTheDocument();
    // Top costly blunder still shown.
    expect(screen.getByText("Qxh7+ vs Re1")).toBeInTheDocument();

    // Removed sections must be gone.
    expect(screen.queryByText("Perfect streak")).not.toBeInTheDocument();
    expect(screen.queryByText("Data Completeness")).not.toBeInTheDocument();
    expect(screen.queryByText("Positions Total")).not.toBeInTheDocument();
  });

  it("renders em dashes for null rates and hides the distribution when there are no moves", async () => {
    getStatsSummaryMock.mockResolvedValueOnce({
      ...baseSummary,
      games: { ...baseSummary.games, played: 0, score_pct: null },
      moves: {
        accuracy_pct: null,
        mistake_free_game_rate: null,
        quality_distribution: null,
      },
      library: {
        ...baseSummary.library,
        blunders_total: 0,
        top_costly_blunders: [],
      },
    });

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText("No games in this window yet. Play a game to start building stats."),
      ).toBeInTheDocument();
    });

    expect(
      screen.getByText("No analyzed moves in this window yet."),
    ).toBeInTheDocument();
    expect(screen.getByText("No blunders captured yet.")).toBeInTheDocument();
    // Null rates render as em dashes (never a misleading 0.0%).
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("hides the Openings section when both lists are empty", async () => {
    getStatsSummaryMock.mockResolvedValueOnce({
      ...baseSummary,
      openings: { strongest: [], weakest: [] },
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Results")).toBeInTheDocument();
    });

    // The nav still links to /openings; assert the section's own sub-headings
    // (unique to the stats Openings block) are absent instead.
    expect(screen.queryByText("Strongest")).not.toBeInTheDocument();
    expect(screen.queryByText("Weakest")).not.toBeInTheDocument();
  });

  it("shows fetch failure and retries successfully", async () => {
    const user = userEvent.setup();
    getStatsSummaryMock.mockRejectedValueOnce(new Error("Stats backend unavailable"));
    getStatsSummaryMock.mockResolvedValueOnce(baseSummary);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Stats backend unavailable")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => {
      expect(screen.getByText("Results")).toBeInTheDocument();
    });

    expect(getStatsSummaryMock).toHaveBeenCalledTimes(2);
    expect(getStatsSummaryMock).toHaveBeenNthCalledWith(1, 30);
    expect(getStatsSummaryMock).toHaveBeenNthCalledWith(2, 30);
  });

  it("refetches when window selector changes", async () => {
    const user = userEvent.setup();
    getStatsSummaryMock.mockResolvedValueOnce(baseSummary);
    getStatsSummaryMock.mockResolvedValueOnce({
      ...baseSummary,
      window_days: 90,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Results")).toBeInTheDocument();
    });

    await user.click(screen.getAllByRole("button", { name: "90d" })[0]);

    await waitFor(() => {
      expect(getStatsSummaryMock).toHaveBeenLastCalledWith(90);
    });

    expect(screen.getAllByRole("button", { name: "90d" })[0]).toHaveAttribute("aria-pressed", "true");
  });

  it("captures stats_window_changed only when the window actually changes", async () => {
    const user = userEvent.setup();
    getStatsSummaryMock.mockResolvedValue(baseSummary);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Results")).toBeInTheDocument();
    });

    // Clicking the already-active 30d button is a no-op for analytics.
    await user.click(screen.getAllByRole("button", { name: "30d" })[0]);
    expect(captureEventMock).not.toHaveBeenCalled();

    await user.click(screen.getAllByRole("button", { name: "90d" })[0]);
    expect(captureEventMock).toHaveBeenCalledWith("stats_window_changed", {
      window_days: 90,
    });
  });

  it("does not re-enter loading when clicking the already-active window button", async () => {
    const user = userEvent.setup();
    getStatsSummaryMock.mockResolvedValue(baseSummary);

    renderPage();

    // Wait for initial load to complete (default is 30d)
    await waitFor(() => {
      expect(screen.getByText("Results")).toBeInTheDocument();
    });

    const fetchCountBefore = getStatsSummaryMock.mock.calls.length;

    // Click the already-active 30d button
    await user.click(screen.getAllByRole("button", { name: "30d" })[0]);

    // Summary should NOT have been re-fetched
    expect(getStatsSummaryMock).toHaveBeenCalledTimes(fetchCountBefore);

    // Page should NOT be in loading state — stats content still visible
    expect(screen.getByText("Results")).toBeInTheDocument();
    expect(screen.queryByText("Loading stats...")).not.toBeInTheDocument();
  });
});
