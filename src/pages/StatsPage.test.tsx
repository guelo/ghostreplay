import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import StatsPage from "./StatsPage";

const mockLogout = vi.fn();
const getStatsSummaryMock = vi.fn();

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
    completed: 10,
    active: 2,
    record: {
      wins: 5,
      losses: 3,
      draws: 2,
      resigns: 1,
      abandons: 1,
    },
    avg_duration_seconds: 3660,
    avg_moves: 37.5,
  },
  colors: {
    white: {
      games: 6,
      completed: 5,
      wins: 3,
      losses: 1,
      draws: 1,
      avg_cpl: 42.5,
      blunders_per_100_moves: 1.2,
    },
    black: {
      games: 6,
      completed: 5,
      wins: 2,
      losses: 2,
      draws: 1,
      avg_cpl: 55.5,
      blunders_per_100_moves: 2.4,
    },
  },
  moves: {
    player_moves: 340,
    avg_cpl: 49.2,
    mistakes_per_100_moves: 6.7,
    blunders_per_100_moves: 1.8,
    quality_distribution: {
      best: 20.5,
      excellent: 24.5,
      good: 28.0,
      inaccuracy: 14.0,
      mistake: 9.0,
      blunder: 4.0,
    },
  },
  library: {
    blunders_total: 73,
    positions_total: 64,
    edges_total: 188,
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
  data_completeness: {
    sessions_with_uploaded_moves_pct: 66.7,
    notes: [
      "Per-move metrics use player moves only.",
      "SRS review stats are excluded until review outcomes are persisted.",
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
  });

  it("loads and renders stats values", async () => {
    getStatsSummaryMock.mockResolvedValueOnce(baseSummary);

    renderPage();

    expect(screen.getByText("Loading stats...")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("Move Quality")).toBeInTheDocument();
    });

    expect(getStatsSummaryMock).toHaveBeenCalledWith(30);
    expect(screen.getByText("Qxh7+ vs Re1")).toBeInTheDocument();
    expect(screen.getByText("66.7%")).toBeInTheDocument();
  });

  it("handles empty and zero data", async () => {
    getStatsSummaryMock.mockResolvedValueOnce({
      ...baseSummary,
      games: {
        ...baseSummary.games,
        played: 0,
        completed: 0,
        active: 0,
        avg_duration_seconds: 0,
        avg_moves: 0,
      },
      moves: {
        ...baseSummary.moves,
        player_moves: 0,
        avg_cpl: 0,
        mistakes_per_100_moves: 0,
        blunders_per_100_moves: 0,
        quality_distribution: {
          best: 0,
          excellent: 0,
          good: 0,
          inaccuracy: 0,
          mistake: 0,
          blunder: 0,
        },
      },
      library: {
        ...baseSummary.library,
        blunders_total: 0,
        positions_total: 0,
        edges_total: 0,
        new_blunders_in_window: 0,
        avg_blunder_eval_loss_cp: 0,
        top_costly_blunders: [],
      },
    });

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText("No games in this window yet. Play a game to start building stats."),
      ).toBeInTheDocument();
    });

    expect(screen.getByText("No blunders captured yet.")).toBeInTheDocument();
    expect(screen.getAllByText("0.0%").length).toBeGreaterThan(0);
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
      expect(screen.getByText("Move Quality")).toBeInTheDocument();
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
      expect(screen.getByText("Games")).toBeInTheDocument();
    });

    await user.click(screen.getAllByRole("button", { name: "90d" })[0]);

    await waitFor(() => {
      expect(getStatsSummaryMock).toHaveBeenLastCalledWith(90);
    });

    expect(screen.getAllByRole("button", { name: "90d" })[0]).toHaveAttribute("aria-pressed", "true");
  });
});
