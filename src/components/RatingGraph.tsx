import { useEffect, useState } from "react";
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

type Range = "7d" | "30d" | "90d" | "all";

const RANGE_OPTIONS: Array<{ label: string; value: Range }> = [
  { label: "7d", value: "7d" },
  { label: "30d", value: "30d" },
  { label: "90d", value: "90d" },
  { label: "All", value: "all" },
];

const PROVISIONAL_THRESHOLD = 20;
const ACCENT = "var(--accent, #7c6fe0)";

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

function RatingGraph() {
  const [range, setRange] = useState<Range>("all");
  const [data, setData] = useState<RatingHistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    fetchRatingHistory(range)
      .then((res) => {
        if (!cancelled) setData(res);
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
  }, [range]);

  const dedupeTickFormatter = (() => {
    let lastLabel = "";
    return (tick: number) => {
      const label = formatDate(new Date(tick).toISOString());
      if (label === lastLabel) return "";
      lastLabel = label;
      return label;
    };
  })();

  const renderChart = () => {
    if (!data || data.ratings.length === 0) return null;

    const chartData = buildChartData(data.ratings);
    const hasProvisional = chartData.some((p) => p.provisionalRating != null);
    const hasStable = chartData.some((p) => p.stableRating != null);

    return (
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={chartData}>
          <CartesianGrid
            strokeDasharray="4 3"
            stroke="var(--border-color)"
            vertical={false}
          />
          <XAxis
            dataKey="date"
            type="number"
            scale="time"
            domain={["auto", "auto"]}
            tickFormatter={dedupeTickFormatter}
            tick={{ fill: "var(--text-tertiary)", fontSize: 11 }}
            stroke="var(--border-color)"
            tickLine={false}
          />
          <YAxis
            domain={["auto", "auto"]}
            tick={{ fill: "var(--text-tertiary)", fontSize: 11 }}
            stroke="var(--border-color)"
            tickLine={false}
            width={44}
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
    );
  };

  return (
    <section className="stats-section">
      <div className="rating-graph__header">
        <h2 className="stats-section__title">Rating</h2>
        <div
          className="rating-graph__range-picker"
          role="group"
          aria-label="Rating time range"
        >
          {RANGE_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              className={`stats-window-picker__button${range === opt.value ? " stats-window-picker__button--active" : ""}`}
              aria-pressed={range === opt.value}
              onClick={() => setRange(opt.value)}
            >
              {opt.label}
            </button>
          ))}
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

        {!loading && !error && data && data.ratings.length > 0 && renderChart()}

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
