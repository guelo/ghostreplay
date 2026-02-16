import { useEffect, useRef, useState } from "react";
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

// Chart layout constants
const PADDING = { top: 20, right: 16, bottom: 32, left: 48 };

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function niceAxis(min: number, max: number, ticks: number): number[] {
  if (min === max) {
    return [min - 50, min, min + 50];
  }
  const range = max - min;
  const rough = range / (ticks - 1);
  const mag = 10 ** Math.floor(Math.log10(rough));
  const nice = [1, 2, 5, 10].find((n) => n * mag >= rough)! * mag;
  const lo = Math.floor(min / nice) * nice;
  const hi = Math.ceil(max / nice) * nice;
  const result: number[] = [];
  for (let v = lo; v <= hi; v += nice) {
    result.push(v);
  }
  return result;
}

function buildPath(
  points: RatingPoint[],
  xScale: (i: number) => number,
  yScale: (v: number) => number,
): string {
  return points
    .map((p, i) => `${i === 0 ? "M" : "L"}${xScale(i)},${yScale(p.rating)}`)
    .join(" ");
}

function RatingGraph() {
  const [range, setRange] = useState<Range>("all");
  const [data, setData] = useState<RatingHistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const height = 260;

  // Responsive width
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setWidth(entry.contentRect.width);
      }
    });
    observer.observe(el);
    setWidth(el.clientWidth);
    return () => observer.disconnect();
  }, []);

  // Fetch data
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

  const chartW = width - PADDING.left - PADDING.right;
  const chartH = height - PADDING.top - PADDING.bottom;

  const renderChart = () => {
    if (!data || data.ratings.length === 0) return null;

    const points = data.ratings;
    const ratings = points.map((p) => p.rating);
    const minR = Math.min(...ratings);
    const maxR = Math.max(...ratings);
    const yTicks = niceAxis(minR, maxR, 5);
    const yMin = yTicks[0];
    const yMax = yTicks[yTicks.length - 1];

    const xScale = (i: number) =>
      PADDING.left +
      (points.length === 1 ? chartW / 2 : (i / (points.length - 1)) * chartW);
    const yScale = (v: number) =>
      PADDING.top +
      chartH -
      (yMax === yMin ? chartH / 2 : ((v - yMin) / (yMax - yMin)) * chartH);

    // Split into provisional and stable segments
    const provEnd = points.findIndex((p) => !p.is_provisional);
    const provPoints = provEnd === -1 ? points : points.slice(0, provEnd + 1);
    const stablePoints =
      provEnd === -1 ? [] : points.slice(provEnd);

    // X-axis labels — pick ~5 evenly spaced
    const xLabelCount = Math.min(5, points.length);
    const xLabels: Array<{ i: number; label: string }> = [];
    for (let k = 0; k < xLabelCount; k++) {
      const i =
        xLabelCount === 1
          ? 0
          : Math.round((k / (xLabelCount - 1)) * (points.length - 1));
      xLabels.push({ i, label: formatDate(points[i].timestamp) });
    }

    return (
      <svg
        ref={svgRef}
        width={width}
        height={height}
        className="rating-graph__svg"
      >
        {/* Y grid + labels */}
        {yTicks.map((tick) => (
          <g key={tick}>
            <line
              x1={PADDING.left}
              y1={yScale(tick)}
              x2={PADDING.left + chartW}
              y2={yScale(tick)}
              className="rating-graph__grid-line"
            />
            <text
              x={PADDING.left - 8}
              y={yScale(tick)}
              className="rating-graph__axis-label"
              textAnchor="end"
              dominantBaseline="middle"
            >
              {tick}
            </text>
          </g>
        ))}

        {/* X labels */}
        {xLabels.map(({ i, label }) => (
          <text
            key={i}
            x={xScale(i)}
            y={height - 6}
            className="rating-graph__axis-label"
            textAnchor="middle"
          >
            {label}
          </text>
        ))}

        {/* Provisional line (dashed) */}
        {provPoints.length > 1 && (
          <path
            d={buildPath(provPoints, xScale, yScale)}
            className="rating-graph__line rating-graph__line--provisional"
            fill="none"
          />
        )}

        {/* Stable line (solid) */}
        {stablePoints.length > 1 && (
          <path
            d={buildPath(
              stablePoints,
              (i) => xScale(i + provEnd),
              yScale,
            )}
            className="rating-graph__line rating-graph__line--stable"
            fill="none"
          />
        )}

        {/* Dots */}
        {points.map((p, i) => (
          <circle
            key={p.game_session_id}
            cx={xScale(i)}
            cy={yScale(p.rating)}
            r={3}
            className={`rating-graph__dot${p.is_provisional ? " rating-graph__dot--provisional" : ""}`}
          >
            <title>
              {p.rating}
              {p.is_provisional ? " (provisional)" : ""} —{" "}
              {formatDate(p.timestamp)}
            </title>
          </circle>
        ))}
      </svg>
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

      <div ref={containerRef} className="rating-graph__container">
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
