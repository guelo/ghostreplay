import { Fragment, useEffect, useMemo, useRef, useState } from "react";
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
  type RatingScoreKey,
} from "../utils/api";
import TimeRangeSlider from "./TimeRangeSlider";

const PROVISIONAL_THRESHOLD = 20;
const SERIES: Array<{ key: RatingScoreKey; label: string; color: string }> = [
  { key: "elo", label: "Elo", color: "var(--accent, #7c6fe0)" },
  { key: "chesscom", label: "Chess.com", color: "#2d8a57" },
  { key: "lichess", label: "Lichess", color: "#c27803" },
];
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
  eloProvisional?: number;
  eloStable?: number;
  chesscomProvisional?: number;
  chesscomStable?: number;
  lichessProvisional?: number;
  lichessStable?: number;
  isProvisional: boolean;
  rating: number;
}

function buildChartData(points: RatingPoint[]): ChartPoint[] {
  const provEnd = points.findIndex((p) => !p.is_provisional);

  return points.map((p, i) => {
    const isProvisional = p.is_provisional;
    const isOverlap = provEnd !== -1 && i === provEnd;

    const scores = p.scores ?? {
      elo: { rating: p.rating, is_provisional: p.is_provisional },
      chesscom: null,
      lichess: null,
    };
    const point: ChartPoint = {
      timestamp: p.timestamp,
      date: new Date(p.timestamp).getTime(),
      rating: p.rating,
      isProvisional,
    };
    for (const series of SERIES) {
      const score = scores[series.key];
      if (!score) continue;
      const provisionalKey = `${series.key}Provisional` as keyof ChartPoint;
      const stableKey = `${series.key}Stable` as keyof ChartPoint;
      if (isProvisional || isOverlap) {
        (point[provisionalKey] as number | undefined) = score.rating;
      }
      if (!isProvisional) {
        (point[stableKey] as number | undefined) = score.rating;
      }
    }
    return point;
  });
}

const hollowDot = (color: string) => (props: Record<string, unknown>) => {
  const { cx, cy, value } = props as { cx: number; cy: number; value?: number };
  if (value == null) return null;
  return (
    <circle
      cx={cx}
      cy={cy}
      r={3}
      fill="none"
      stroke={color}
      strokeWidth={1.5}
    />
  );
};

const filledDot = (color: string) => (props: Record<string, unknown>) => {
  const { cx, cy, value } = props as { cx: number; cy: number; value?: number };
  if (value == null) return null;
  return <circle cx={cx} cy={cy} r={3} fill={color} />;
};

const CustomTooltip = ({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: ChartPoint; name?: string; value?: number; color?: string }>;
}) => {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  const values = payload.filter((item) => item.value != null);
  return (
    <div className="rating-graph__tooltip">
      {values.map((item) => (
        <div key={`${item.name}-${item.value}`}>
          <strong style={{ color: item.color }}>{item.value}</strong> {item.name}
        </div>
      ))}
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

function computeViewRange(
  dataMin: number,
  domainMax: number,
  windowDays: number,
): [number, number] {
  const span = domainMax - dataMin;
  if (windowDays === 0 || span <= 0) return [0, 1];
  const cutoff = domainMax - windowDays * DAY_MS;
  return [Math.max(0, (cutoff - dataMin) / span), 1];
}

function rangesEqual(
  a: [number, number],
  b: [number, number],
): boolean {
  return a[0] === b[0] && a[1] === b[1];
}

function RatingGraph({ windowDays, presetKey }: RatingGraphProps) {
  const [showProvisional, setShowProvisional] = useState(true);
  const [visibleSeries, setVisibleSeries] = useState<Record<RatingScoreKey, boolean>>({
    elo: true,
    chesscom: true,
    lichess: true,
  });
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
          const stableRatings = res.ratings.filter((r) => !r.is_provisional);
          const nextShowProvisional = stableRatings.length <= 3;
          const initialRatings = nextShowProvisional
            ? res.ratings
            : stableRatings;
          const initialChartData = buildChartData(initialRatings);
          const initialDataMin = initialChartData[0]?.date ?? 0;
          const initialRawDataMax = initialChartData.at(-1)?.date ?? 0;
          const initialDomainMax = Math.max(initialRawDataMax, Date.now());

          domainMaxRef.current = initialDomainMax;
          setViewRange((prev) => {
            const next = computeViewRange(
              initialDataMin,
              initialDomainMax,
              windowDays,
            );
            return rangesEqual(prev, next) ? prev : next;
          });
          setShowProvisional(nextShowProvisional);
          setData(res);
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
        ? (showProvisional
            ? data.ratings
            : data.ratings.filter((r) => !r.is_provisional)
          ).filter((r) => SERIES.some((series) => visibleSeries[series.key] && (r.scores?.[series.key] ?? (series.key === "elo" ? { rating: r.rating } : null))))
        : [],
    [data, showProvisional, visibleSeries],
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
    if (!hasChartData) return;
    const next = computeViewRange(
      dataMinRef.current,
      domainMaxRef.current,
      windowDays,
    );
    setViewRange((prev) => (rangesEqual(prev, next) ? prev : next));
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

  const hasAnySeriesChecked = SERIES.some((series) => visibleSeries[series.key]);
  const hasSelectedSeriesData = filteredRatings.length > 0;
  const showSlider = allChartData.length > 1;
  const currentVisibleScores = useMemo(() => {
    if (!data?.scores) return [];
    return SERIES.flatMap((series) => {
      if (!visibleSeries[series.key]) return [];
      const score = data.scores?.[series.key];
      return score ? [{ ...series, score }] : [];
    });
  }, [data, visibleSeries]);

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
            {SERIES.map((series) => {
              if (!visibleSeries[series.key]) return null;
              const provisionalKey = `${series.key}Provisional`;
              const stableKey = `${series.key}Stable`;
              const hasProvisional = visibleData.some((p) => (p as unknown as Record<string, unknown>)[provisionalKey] != null);
              const hasStable = visibleData.some((p) => (p as unknown as Record<string, unknown>)[stableKey] != null);
              return (
                <Fragment key={series.key}>
                  {hasProvisional && (
                    <Line
                      name={series.label}
                      type="linear"
                      dataKey={provisionalKey}
                      stroke={series.color}
                      strokeDasharray="6 4"
                      strokeWidth={2}
                      opacity={0.6}
                      dot={hollowDot(series.color)}
                      activeDot={false}
                      connectNulls
                      isAnimationActive={false}
                    />
                  )}
                  {hasStable && (
                    <Line
                      name={series.label}
                      type="linear"
                      dataKey={stableKey}
                      stroke={series.color}
                      strokeWidth={2}
                      dot={filledDot(series.color)}
                      activeDot={false}
                      connectNulls
                      isAnimationActive={false}
                    />
                  )}
                </Fragment>
              );
            })}
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
          <div className="rating-graph__series" role="group" aria-label="Rating series">
            {SERIES.map((series) => (
              <label key={series.key} className="rating-graph__toggle">
                <input
                  type="checkbox"
                  checked={visibleSeries[series.key]}
                  onChange={(e) =>
                    setVisibleSeries((prev) => ({
                      ...prev,
                      [series.key]: e.target.checked,
                    }))
                  }
                />
                {series.label}
              </label>
            ))}
          </div>
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
            {!hasAnySeriesChecked
              ? "Select at least one rating series."
              : showProvisional
                ? "No data for the selected rating series yet."
                : "No stable ratings yet. Complete more games to see your rating!"}
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

        {!loading && !error && data && hasSelectedSeriesData && renderChart()}

        {!loading && !error && data && data.ratings.length > 0 && currentVisibleScores.length > 0 && (
          <p className="rating-graph__current">
            Current{" "}
            {currentVisibleScores.map((series, index) => (
              <span key={series.key}>
                {index > 0 ? " · " : ""}
                {series.label}:{" "}
                <strong style={{ color: series.color }}>{series.score.rating}</strong>
                {series.score.is_provisional ? "?" : ""}
              </span>
            ))}
          </p>
        )}
      </div>
    </section>
  );
}

export default RatingGraph;
