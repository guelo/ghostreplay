import { useEffect, useMemo, useRef, useState } from "react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from "recharts";
import {
  fetchRatingHistory,
  type RatingHistoryResponse,
  type RatingPoint,
} from "../utils/api";
import TimeRangeSlider from "./TimeRangeSlider";

const PROVISIONAL_THRESHOLD = 20;
const ACCENT = "var(--accent, #7c6fe0)";
const DAY_MS = 86_400_000;

export const CHART_LAYOUT = {
  marginLeft: 8,
  marginRight: 12,
  yAxisWidth: 44,
  marginTop: 5,
  marginBottom: 0,
} as const;

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

interface ChartPoint {
  timestamp: string;
  date: number;
  provisionalRating?: number;
  stableRating?: number;
  isProvisional: boolean;
  rating: number;
}

function buildChartData(points: RatingPoint[]): ChartPoint[] {
  const provEnd = points.findIndex((p) => !p.is_provisional);

  return points.map((p, i) => {
    const isProvisional = p.is_provisional;
    const isOverlap = provEnd !== -1 && i === provEnd;

    return {
      timestamp: p.timestamp,
      date: new Date(p.timestamp).getTime(),
      rating: p.rating,
      isProvisional,
      provisionalRating: isProvisional || isOverlap ? p.rating : undefined,
      stableRating: !isProvisional ? p.rating : undefined,
    };
  });
}

const hollowDot = (props: Record<string, unknown>) => {
  const { cx, cy, value } = props as { cx: number; cy: number; value?: number };
  if (value == null) return null;
  return (
    <circle
      cx={cx}
      cy={cy}
      r={3}
      fill="none"
      stroke={ACCENT}
      strokeWidth={1.5}
    />
  );
};

const filledDot = (props: Record<string, unknown>) => {
  const { cx, cy, value } = props as { cx: number; cy: number; value?: number };
  if (value == null) return null;
  return <circle cx={cx} cy={cy} r={3} fill={ACCENT} />;
};

const CustomTooltip = ({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: ChartPoint }>;
}) => {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="rating-graph__tooltip">
      <strong>{point.rating}</strong>
      {point.isProvisional ? " (provisional)" : ""}
      <br />
      {formatDate(point.timestamp)}
    </div>
  );
};

interface RatingGraphProps {
  windowDays: number;
  presetKey: number;
}

function computeTickFormat(visibleSpanDays: number) {
  if (visibleSpanDays < 14) {
    return (ts: number) =>
      new Date(ts).toLocaleDateString(undefined, {
        weekday: "short",
        day: "numeric",
      });
  }
  if (visibleSpanDays < 90) {
    return (ts: number) =>
      new Date(ts).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      });
  }
  if (visibleSpanDays < 365) {
    return (ts: number) =>
      new Date(ts).toLocaleDateString(undefined, { month: "short" });
  }
  return (ts: number) =>
    new Date(ts).toLocaleDateString(undefined, {
      month: "short",
      year: "2-digit",
    });
}

function RatingGraph({ windowDays, presetKey }: RatingGraphProps) {
  const [showProvisional, setShowProvisional] = useState(true);
  const [provisionalDefaultSet, setProvisionalDefaultSet] = useState(false);
  const [data, setData] = useState<RatingHistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [viewRange, setViewRange] = useState<[number, number]>([0, 1]);
  const [containerWidth, setContainerWidth] = useState(400);
  const containerRef = useRef<HTMLDivElement>(null);

  // Fetch all data once on mount
  useEffect(() => {
    let cancelled = false;

    fetchRatingHistory("all")
      .then((res) => {
        if (!cancelled) {
          setData(res);
          if (!provisionalDefaultSet) {
            const stableCount = res.ratings.filter(
              (r) => !r.is_provisional,
            ).length;
            if (stableCount > 3) setShowProvisional(false);
            setProvisionalDefaultSet(true);
          }
        }
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "Failed to load");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  // Measure container width for tick computation
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      setContainerWidth(entry.contentRect.width);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const filteredRatings = useMemo(
    () =>
      data
        ? showProvisional
          ? data.ratings
          : data.ratings.filter((r) => !r.is_provisional)
        : [],
    [data, showProvisional],
  );

  const allChartData = useMemo(
    () => buildChartData(filteredRatings),
    [filteredRatings],
  );

  const dataMin = allChartData[0]?.date ?? 0;
  const rawDataMax = allChartData.at(-1)?.date ?? 0;

  // domainMax must be stable across renders to avoid infinite loops
  // (Date.now() drifts → span changes → snap effect → setViewRange → re-render).
  // Re-anchor only when data changes or a preset click occurs.
  const domainMaxRef = useRef(0);
  const domainMax = useMemo(() => {
    const fresh = Math.max(rawDataMax, Date.now());
    domainMaxRef.current = fresh;
    return fresh;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rawDataMax, presetKey]);

  const span = domainMax - dataMin;
  const minFraction = span > DAY_MS ? DAY_MS / span : 1;
  const hasChartData = allChartData.length > 0;

  // Refs let the snap effect read current values without depending on them,
  // so the provisional toggle changing dataMin/span won't re-trigger a snap.
  const dataMinRef = useRef(dataMin);
  const spanRef = useRef(span);
  dataMinRef.current = dataMin;
  spanRef.current = span;

  useEffect(() => {
    if (!hasChartData || spanRef.current <= 0) return;
    if (windowDays === 0) {
      setViewRange([0, 1]);
      return;
    }
    const cutoff = domainMaxRef.current - windowDays * DAY_MS;
    const frac = Math.max(0, (cutoff - dataMinRef.current) / spanRef.current);
    setViewRange([frac, 1]);
    // Triggers: initial data load, preset button click.
    // NOT triggered by dataMin/span changes (provisional toggle).
  }, [windowDays, presetKey, hasChartData]);

  const viewStart = dataMin + viewRange[0] * span;
  const viewEnd = dataMin + viewRange[1] * span;

  const visibleData = useMemo(
    () =>
      allChartData.filter((p, i, arr) => {
        if (p.date >= viewStart && p.date <= viewEnd) return true;
        if (i > 0 && arr[i - 1].date < viewEnd && p.date > viewEnd)
          return true;
        if (
          i < arr.length - 1 &&
          arr[i + 1].date > viewStart &&
          p.date < viewStart
        )
          return true;
        return false;
      }),
    [allChartData, viewStart, viewEnd],
  );

  const plotWidth =
    containerWidth -
    CHART_LAYOUT.yAxisWidth -
    CHART_LAYOUT.marginLeft -
    CHART_LAYOUT.marginRight;

  const { ticks, tickFormatter } = useMemo(() => {
    const visibleSpanMs = viewEnd - viewStart;
    const visibleSpanDays = visibleSpanMs / DAY_MS;
    const maxTicks = Math.max(2, Math.floor(plotWidth / 80));
    const step = visibleSpanMs / (maxTicks + 1);
    const fmt = computeTickFormat(visibleSpanDays);

    // Generate evenly-spaced candidates, then deduplicate adjacent labels
    // (e.g. two ticks in the same month both format to "Feb" in the 90-365d tier)
    const positions: number[] = [];
    let prevLabel = "";
    for (let i = 1; i <= maxTicks; i++) {
      const ts = viewStart + step * i;
      const label = fmt(ts);
      if (label !== prevLabel) {
        positions.push(ts);
        prevLabel = label;
      }
    }

    return { ticks: positions, tickFormatter: fmt };
  }, [viewStart, viewEnd, plotWidth]);

  const hasProvisional = visibleData.some((p) => p.provisionalRating != null);
  const hasStable = visibleData.some((p) => p.stableRating != null);
  const showSlider = allChartData.length > 1;

  const renderChart = () => {
    if (visibleData.length === 0 && allChartData.length === 0) return null;

    return (
      <div ref={containerRef} style={{ width: "100%" }}>
        <ResponsiveContainer width="100%" height={260}>
          <LineChart
            data={visibleData}
            margin={{
              left: CHART_LAYOUT.marginLeft,
              right: CHART_LAYOUT.marginRight,
              top: CHART_LAYOUT.marginTop,
              bottom: CHART_LAYOUT.marginBottom,
            }}
          >
            <CartesianGrid
              strokeDasharray="4 3"
              stroke="var(--border-color)"
              vertical={false}
            />
            <XAxis
              dataKey="date"
              type="number"
              scale="time"
              domain={[viewStart, viewEnd]}
              ticks={ticks}
              tickFormatter={tickFormatter}
              tick={{ fill: "var(--text-tertiary)", fontSize: 11 }}
              stroke="var(--border-color)"
              tickLine={false}
            />
            <YAxis
              domain={["auto", "auto"]}
              tick={{ fill: "var(--text-tertiary)", fontSize: 11 }}
              stroke="var(--border-color)"
              tickLine={false}
              width={CHART_LAYOUT.yAxisWidth}
            />
            <Tooltip
              content={<CustomTooltip />}
              cursor={{ stroke: "var(--border-color)" }}
            />
            {hasProvisional && (
              <Line
                type="linear"
                dataKey="provisionalRating"
                stroke={ACCENT}
                strokeDasharray="6 4"
                strokeWidth={2}
                opacity={0.6}
                dot={hollowDot}
                activeDot={false}
                connectNulls
                isAnimationActive={false}
              />
            )}
            {hasStable && (
              <Line
                type="linear"
                dataKey="stableRating"
                stroke={ACCENT}
                strokeWidth={2}
                dot={filledDot}
                activeDot={false}
                connectNulls
                isAnimationActive={false}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
        {showSlider && (
          <TimeRangeSlider
            value={viewRange}
            onChange={setViewRange}
            paddingLeft={CHART_LAYOUT.yAxisWidth + CHART_LAYOUT.marginLeft}
            paddingRight={CHART_LAYOUT.marginRight}
            minFraction={minFraction}
          />
        )}
      </div>
    );
  };

  return (
    <section className="stats-section">
      <div className="rating-graph__header">
        <div className="rating-graph__header-left">
          <h2 className="stats-section__title">Rating</h2>
          {data && data.ratings.some((r) => r.is_provisional) && (
            <label className="rating-graph__toggle">
              <input
                type="checkbox"
                checked={showProvisional}
                onChange={(e) => setShowProvisional(e.target.checked)}
              />
              Show provisional
            </label>
          )}
        </div>
      </div>

      <div className="rating-graph__container">
        {loading && (
          <p className="stats-shell__placeholder">Loading rating...</p>
        )}

        {!loading && error && (
          <p className="stats-shell__error">{error}</p>
        )}

        {!loading && !error && data && data.ratings.length === 0 && (
          <p className="rating-graph__empty">
            No rated games yet. Complete a game to start tracking your rating.
          </p>
        )}

        {!loading && !error && data && data.ratings.length > 0 && filteredRatings.length === 0 && (
          <p className="rating-graph__empty">
            No stable ratings yet. Complete more games to see your rating!
          </p>
        )}

        {!loading &&
          !error &&
          data &&
          data.ratings.length > 0 &&
          data.games_played < PROVISIONAL_THRESHOLD && (
            <p className="rating-graph__provisional-note">
              Provisional rating ({data.games_played}/{PROVISIONAL_THRESHOLD}{" "}
              games). Your rating will stabilize as you play more.
            </p>
          )}

        {!loading && !error && data && filteredRatings.length > 0 && renderChart()}

        {!loading && !error && data && data.ratings.length > 0 && (
          <p className="rating-graph__current">
            Current: <strong>{data.current_rating}</strong>
            {data.games_played < PROVISIONAL_THRESHOLD ? "?" : ""}
          </p>
        )}
      </div>
    </section>
  );
}

export default RatingGraph;
