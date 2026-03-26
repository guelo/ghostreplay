import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { RatingHistoryResponse, RatingPoint } from "../utils/api";

const fetchRatingHistoryMock = vi.fn();

vi.mock("../utils/api", () => ({
  fetchRatingHistory: (...args: unknown[]) => fetchRatingHistoryMock(...args),
}));

// Prop-capturing recharts stubs
const lineChartPropsLog: Array<Record<string, unknown>> = [];
const xAxisPropsLog: Array<Record<string, unknown>> = [];

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="responsive-container">{children}</div>
  ),
  LineChart: (props: Record<string, unknown>) => {
    lineChartPropsLog.push(props);
    return (
      <div data-testid="line-chart">{props.children as React.ReactNode}</div>
    );
  },
  Line: () => <div />,
  XAxis: (props: Record<string, unknown>) => {
    xAxisPropsLog.push(props);
    return <div data-testid="x-axis" />;
  },
  YAxis: () => <div />,
  CartesianGrid: () => <div />,
  Tooltip: () => <div />,
}));

// Lazy import so mocks are in place
const { default: RatingGraph, CHART_LAYOUT } = await import("./RatingGraph");

const DAY_MS = 86_400_000;
const PINNED_NOW = new Date("2026-03-01T12:00:00Z").getTime();

function makePoint(
  rating: number,
  isProvisional: boolean,
  dayOffset: number,
): RatingPoint {
  const d = new Date(2026, 1, 1 + dayOffset); // Feb 1 + offset
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

describe("RatingGraph", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(PINNED_NOW);
    fetchRatingHistoryMock.mockReset();
    lineChartPropsLog.length = 0;
    xAxisPropsLog.length = 0;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  describe("provisional defaults", () => {
    it("defaults showProvisional to OFF when >3 stable points", async () => {
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(5, 4));
      render(<RatingGraph windowDays={0} presetKey={0} />);

      await waitFor(() => {
        expect(screen.getByLabelText("Show provisional")).toBeInTheDocument();
      });

      const checkbox = screen.getByLabelText(
        "Show provisional",
      ) as HTMLInputElement;
      expect(checkbox.checked).toBe(false);
    });

    it("defaults showProvisional to ON when ≤3 stable points", async () => {
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(5, 3));
      render(<RatingGraph windowDays={0} presetKey={0} />);

      await waitFor(() => {
        expect(screen.getByLabelText("Show provisional")).toBeInTheDocument();
      });

      const checkbox = screen.getByLabelText(
        "Show provisional",
      ) as HTMLInputElement;
      expect(checkbox.checked).toBe(true);
    });

    it("defaults showProvisional to ON when all points are provisional", async () => {
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(10, 0));
      render(<RatingGraph windowDays={0} presetKey={0} />);

      await waitFor(() => {
        expect(screen.getByLabelText("Show provisional")).toBeInTheDocument();
      });

      const checkbox = screen.getByLabelText(
        "Show provisional",
      ) as HTMLInputElement;
      expect(checkbox.checked).toBe(true);
    });
  });

  describe("data fetching", () => {
    it("fetches all data once on mount regardless of windowDays", async () => {
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(5, 4));
      const { rerender } = render(
        <RatingGraph windowDays={7} presetKey={0} />,
      );

      await waitFor(() => {
        expect(fetchRatingHistoryMock).toHaveBeenCalledWith("all");
      });

      rerender(<RatingGraph windowDays={30} presetKey={1} />);

      // Still only called once
      expect(fetchRatingHistoryMock).toHaveBeenCalledTimes(1);
    });
  });

  describe("chart prop integration", () => {
    it("passes explicit margins matching CHART_LAYOUT", async () => {
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(0, 10));
      render(<RatingGraph windowDays={0} presetKey={0} />);

      await waitFor(() => {
        expect(lineChartPropsLog.length).toBeGreaterThan(0);
      });

      const lastProps = lineChartPropsLog.at(-1)!;
      const margin = lastProps.margin as Record<string, number>;
      expect(margin.left).toBe(CHART_LAYOUT.marginLeft);
      expect(margin.right).toBe(CHART_LAYOUT.marginRight);
      expect(margin.top).toBe(CHART_LAYOUT.marginTop);
      expect(margin.bottom).toBe(CHART_LAYOUT.marginBottom);
    });

    it("sets XAxis domain from windowDays using calendar anchor", async () => {
      // Data spans Feb 1 to Feb 25 (24 days of stable data)
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(0, 25));
      render(<RatingGraph windowDays={7} presetKey={0} />);

      await waitFor(() => {
        expect(xAxisPropsLog.length).toBeGreaterThan(0);
      });

      const lastXAxis = xAxisPropsLog.at(-1)!;
      const domain = lastXAxis.domain as [number, number];
      const expectedCutoff = PINNED_NOW - 7 * DAY_MS;

      // domain[0] should be near the cutoff (Feb 22)
      // domain[1] should be domainMax (Date.now() = Mar 1 since it's after dataMax Feb 25)
      expect(domain[0]).toBeCloseTo(expectedCutoff, -3); // within ~1 second
      expect(domain[1]).toBeCloseTo(PINNED_NOW, -3);
    });

    it("sets XAxis domain to full range when windowDays=0", async () => {
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(0, 25));
      render(<RatingGraph windowDays={0} presetKey={0} />);

      await waitFor(() => {
        expect(xAxisPropsLog.length).toBeGreaterThan(0);
      });

      const lastXAxis = xAxisPropsLog.at(-1)!;
      const domain = lastXAxis.domain as [number, number];
      const dataMin = new Date(2026, 1, 1).getTime();

      expect(domain[0]).toBeCloseTo(dataMin, -3);
      expect(domain[1]).toBeCloseTo(PINNED_NOW, -3);
    });

    it("provides ticks array within domain bounds", async () => {
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(0, 25));
      render(<RatingGraph windowDays={0} presetKey={0} />);

      await waitFor(() => {
        expect(xAxisPropsLog.length).toBeGreaterThan(0);
      });

      const lastXAxis = xAxisPropsLog.at(-1)!;
      const ticks = lastXAxis.ticks as number[];
      const domain = lastXAxis.domain as [number, number];

      expect(ticks.length).toBeGreaterThanOrEqual(2);
      for (const t of ticks) {
        expect(t).toBeGreaterThan(domain[0]);
        expect(t).toBeLessThan(domain[1]);
      }
    });

    it("provides a tickFormatter function", async () => {
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(0, 25));
      render(<RatingGraph windowDays={0} presetKey={0} />);

      await waitFor(() => {
        expect(xAxisPropsLog.length).toBeGreaterThan(0);
      });

      const lastXAxis = xAxisPropsLog.at(-1)!;
      const fmt = lastXAxis.tickFormatter as (ts: number) => string;

      expect(typeof fmt).toBe("function");
      const result = fmt(PINNED_NOW);
      expect(typeof result).toBe("string");
      expect(result.length).toBeGreaterThan(0);
    });
  });

  describe("presetKey re-snap", () => {
    it("bumping presetKey re-snaps domain with fresh Date.now even when windowDays is unchanged", async () => {
      // 20 stable points: Feb 1 – Feb 20 (ends before PINNED_NOW Mar 1,
      // so domainMax = Date.now() and shifts when system time advances)
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(0, 20));
      const { rerender } = render(
        <RatingGraph windowDays={7} presetKey={0} />,
      );

      await waitFor(() => {
        expect(xAxisPropsLog.length).toBeGreaterThan(0);
      });

      const domain7dInitial = (
        xAxisPropsLog.at(-1)!.domain as [number, number]
      ).slice();

      // Advance system time by 2 days — simulates the page being left open
      vi.setSystemTime(PINNED_NOW + 2 * DAY_MS);

      // Rerender with SAME windowDays but bumped presetKey
      // This should re-snap using the new Date.now() (2 days later),
      // shifting the 7d window forward.
      xAxisPropsLog.length = 0;
      rerender(<RatingGraph windowDays={7} presetKey={1} />);

      await waitFor(() => {
        expect(xAxisPropsLog.length).toBeGreaterThan(0);
      });

      const domain7dResnapped = xAxisPropsLog.at(-1)!.domain as [number, number];

      // domainMax refreshed → domain end should have shifted forward by ~2 days
      expect(domain7dResnapped[1]).toBeGreaterThan(domain7dInitial[1]);
      // The 7d cutoff also shifted → domain start should have shifted forward
      expect(domain7dResnapped[0]).toBeGreaterThan(domain7dInitial[0]);
    });
  });

  describe("slider drag then preset re-snap integration", () => {
    it("dragging the slider changes the domain, then bumping presetKey restores it", async () => {
      // 20 stable points: Feb 1 – Feb 20 (ends before PINNED_NOW)
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(0, 20));
      const { rerender } = render(
        <RatingGraph windowDays={7} presetKey={0} />,
      );

      await waitFor(() => {
        expect(xAxisPropsLog.length).toBeGreaterThan(0);
      });

      const presetDomain = (
        xAxisPropsLog.at(-1)!.domain as [number, number]
      ).slice();

      // The slider's track needs a measured width for pointer drag math
      const track = document.querySelector(
        ".time-slider__track",
      ) as HTMLElement;
      expect(track).not.toBeNull();
      vi.spyOn(track, "getBoundingClientRect").mockReturnValue({
        width: 400,
        height: 28,
        top: 0,
        left: 0,
        right: 400,
        bottom: 28,
        x: 0,
        y: 0,
        toJSON: () => {},
      });

      // Drag the left handle rightward by 100px (= 0.25 of 400px track)
      // to narrow the visible window
      const leftHandle = screen.getByRole("slider", { name: "Range start" });
      const sliderOuter = track.parentElement!;

      fireEvent.pointerDown(leftHandle, { clientX: 0, pointerId: 1 });
      fireEvent.pointerMove(sliderOuter, { clientX: 100, pointerId: 1 });
      fireEvent.pointerUp(sliderOuter, { pointerId: 1 });

      // Domain should have changed — start moved rightward
      await waitFor(() => {
        const lastDomain = xAxisPropsLog.at(-1)!.domain as [number, number];
        expect(lastDomain[0]).toBeGreaterThan(presetDomain[0]);
      });

      const draggedDomain = (
        xAxisPropsLog.at(-1)!.domain as [number, number]
      ).slice();

      // Now re-snap: same windowDays, bumped presetKey
      xAxisPropsLog.length = 0;
      rerender(<RatingGraph windowDays={7} presetKey={1} />);

      await waitFor(() => {
        expect(xAxisPropsLog.length).toBeGreaterThan(0);
      });

      const resnappedDomain = xAxisPropsLog.at(-1)!.domain as [number, number];

      // Domain should be restored to the preset, not the dragged position
      expect(resnappedDomain[0]).toBeCloseTo(presetDomain[0], -3);
      expect(resnappedDomain[0]).toBeLessThan(draggedDomain[0]);
    });
  });

  describe("user toggle with props", () => {
    it("does not override user toggle on provisional checkbox", async () => {
      const user = userEvent.setup();
      fetchRatingHistoryMock.mockResolvedValue(makeResponse(5, 4));
      render(<RatingGraph windowDays={0} presetKey={0} />);

      await waitFor(() => {
        expect(screen.getByLabelText("Show provisional")).toBeInTheDocument();
      });

      const checkbox = screen.getByLabelText(
        "Show provisional",
      ) as HTMLInputElement;
      expect(checkbox.checked).toBe(false);

      // User manually checks it
      await user.click(checkbox);
      expect(checkbox.checked).toBe(true);
    });
  });
});
