import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RatingHistoryResponse, RatingPoint } from "../utils/api";

const fetchRatingHistoryMock = vi.fn();

vi.mock("../utils/api", () => ({
  fetchRatingHistory: (...args: unknown[]) => fetchRatingHistoryMock(...args),
}));

// Minimal recharts stubs – render children / data-testid wrappers
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="responsive-container">{children}</div>
  ),
  LineChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="line-chart">{children}</div>
  ),
  Line: () => <div />,
  XAxis: () => <div />,
  YAxis: () => <div />,
  CartesianGrid: () => <div />,
  Tooltip: () => <div />,
}));

// Lazy import so mocks are in place
const { default: RatingGraph } = await import("./RatingGraph");

function makePoint(
  rating: number,
  isProvisional: boolean,
  dayOffset: number,
): RatingPoint {
  const d = new Date(2026, 1, 1 + dayOffset);
  return {
    timestamp: d.toISOString(),
    rating,
    is_provisional: isProvisional,
    game_session_id: `gs-${dayOffset}`,
  };
}

function makeResponse(
  provisionalCount: number,
  stableCount: number,
): RatingHistoryResponse {
  const ratings: RatingPoint[] = [];
  for (let i = 0; i < provisionalCount; i++) {
    ratings.push(makePoint(1000 + i * 10, true, i));
  }
  for (let i = 0; i < stableCount; i++) {
    ratings.push(makePoint(1100 + i * 10, false, provisionalCount + i));
  }
  return {
    ratings,
    current_rating: ratings.length ? ratings[ratings.length - 1].rating : 1200,
    games_played: provisionalCount + stableCount,
  };
}

describe("RatingGraph provisional default", () => {
  beforeEach(() => {
    fetchRatingHistoryMock.mockReset();
  });

  it("defaults showProvisional to OFF when >3 stable points", async () => {
    // 5 provisional + 4 stable → checkbox should be unchecked
    fetchRatingHistoryMock.mockResolvedValue(makeResponse(5, 4));

    render(<RatingGraph />);

    await waitFor(() => {
      expect(screen.getByLabelText("Show provisional")).toBeInTheDocument();
    });

    const checkbox = screen.getByLabelText("Show provisional") as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
  });

  it("defaults showProvisional to ON when ≤3 stable points", async () => {
    // 5 provisional + 3 stable → checkbox should stay checked
    fetchRatingHistoryMock.mockResolvedValue(makeResponse(5, 3));

    render(<RatingGraph />);

    await waitFor(() => {
      expect(screen.getByLabelText("Show provisional")).toBeInTheDocument();
    });

    const checkbox = screen.getByLabelText("Show provisional") as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
  });

  it("defaults showProvisional to ON when all points are provisional", async () => {
    fetchRatingHistoryMock.mockResolvedValue(makeResponse(10, 0));

    render(<RatingGraph />);

    await waitFor(() => {
      expect(screen.getByLabelText("Show provisional")).toBeInTheDocument();
    });

    const checkbox = screen.getByLabelText("Show provisional") as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
  });

  it("does not override user toggle when range changes", async () => {
    const user = userEvent.setup();
    // First load: >3 stable → auto-unchecked
    fetchRatingHistoryMock.mockResolvedValue(makeResponse(5, 4));

    render(<RatingGraph />);

    await waitFor(() => {
      expect(screen.getByLabelText("Show provisional")).toBeInTheDocument();
    });

    const checkbox = screen.getByLabelText("Show provisional") as HTMLInputElement;
    expect(checkbox.checked).toBe(false);

    // User manually checks it
    await user.click(checkbox);
    expect(checkbox.checked).toBe(true);

    // Change range → new fetch
    fetchRatingHistoryMock.mockResolvedValue(makeResponse(5, 10));
    await user.click(screen.getByRole("button", { name: "7d" }));

    await waitFor(() => {
      expect(fetchRatingHistoryMock).toHaveBeenCalledWith("7d");
    });

    // Checkbox should still reflect user's manual choice
    await waitFor(() => {
      expect(
        (screen.getByLabelText("Show provisional") as HTMLInputElement).checked,
      ).toBe(true);
    });
  });
});
