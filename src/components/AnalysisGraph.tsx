import { memo, useCallback, useId, useRef, useMemo } from "react";

type HighlightedMoves = {
  indices: number[];
  classification: 'blunder' | 'mistake' | 'inaccuracy';
};

type AnalysisGraphProps = {
  evals: (number | null)[];
  currentIndex: number | null;
  onSelectMove: (index: number) => void;
  playerColor?: "white" | "black";
  evalCp?: number | null;
  isCheckmate?: boolean;
  streamingEval?: { index: number; cp: number } | null;
  pendingIndices?: number[];
  highlightedMoves?: HighlightedMoves | null;
};

const SVG_WIDTH = 600;
const SVG_HEIGHT = 120;
const PAD_X = 8;
const PAD_X_RIGHT = 0;
const PAD_Y = 4;

/**
 * Signed log scale: maps centipawns to a visually compressed value.
 * Uses log(1 + |cp|/100) so that:
 *   - 0cp → 0
 *   - ±100cp (1 pawn) → ±0.69
 *   - ±500cp (5 pawns) → ±1.79
 *   - ±9990cp (~mate)  → ±4.61
 * This keeps early-game detail visible even when late-game evals explode.
 */
const logScale = (cp: number) =>
  Math.sign(cp) * Math.log1p(Math.abs(cp) / 100);

const EVAL_COLOR_LOSING: [number, number, number] = [255, 59, 48]; // #FF3B30
const EVAL_COLOR_EQUAL: [number, number, number] = [158, 158, 158]; // #9E9E9E
const EVAL_COLOR_WINNING: [number, number, number] = [0, 200, 83]; // #00C853

function evalToColor(
  evalCp: number,
  playerColor: "white" | "black",
): string {
  const userCp = playerColor === "white" ? evalCp : -evalCp;
  const clamped = Math.max(-500, Math.min(500, userCp));
  const t = (clamped + 500) / 1000; // 0 = losing, 1 = winning

  let r: number, g: number, b: number;
  if (t < 0.5) {
    const s = t * 2;
    r = EVAL_COLOR_LOSING[0] + (EVAL_COLOR_EQUAL[0] - EVAL_COLOR_LOSING[0]) * s;
    g = EVAL_COLOR_LOSING[1] + (EVAL_COLOR_EQUAL[1] - EVAL_COLOR_LOSING[1]) * s;
    b = EVAL_COLOR_LOSING[2] + (EVAL_COLOR_EQUAL[2] - EVAL_COLOR_LOSING[2]) * s;
  } else {
    const s = (t - 0.5) * 2;
    r = EVAL_COLOR_EQUAL[0] + (EVAL_COLOR_WINNING[0] - EVAL_COLOR_EQUAL[0]) * s;
    g = EVAL_COLOR_EQUAL[1] + (EVAL_COLOR_WINNING[1] - EVAL_COLOR_EQUAL[1]) * s;
    b = EVAL_COLOR_EQUAL[2] + (EVAL_COLOR_WINNING[2] - EVAL_COLOR_EQUAL[2]) * s;
  }

  return `rgba(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)}, 0.39)`;
}

const formatEval = (cp: number) => {
  const sign = cp > 0 ? "+" : "";
  return `${sign}${(cp / 100).toFixed(1)}`;
};

const AnalysisGraph = ({
  evals,
  currentIndex,
  onSelectMove,
  playerColor,
  evalCp,
  isCheckmate,
  streamingEval,
  pendingIndices,
  highlightedMoves,
}: AnalysisGraphProps) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const clipId = useId();

  const n = evals.length;
  // Total moves includes pending ones for x-axis spacing
  const totalMoves = useMemo(() => {
    if (!pendingIndices || pendingIndices.length === 0) return n;
    const maxPending = Math.max(...pendingIndices);
    return Math.max(n, maxPending + 1);
  }, [n, pendingIndices]);

  const chartW = SVG_WIDTH - PAD_X - PAD_X_RIGHT;
  const chartH = SVG_HEIGHT - PAD_Y * 2;
  const midY = PAD_Y + chartH / 2;

  // Compute maxLog from confirmed evals only — streaming eval is excluded so
  // confirmed geometry (points, paths) stays frozen across streaming ticks.
  const maxLog = useMemo(() => {
    let ml = logScale(200); // minimum range ≈ ±2 pawns
    for (const ev of evals) {
      if (ev != null) {
        const v = Math.abs(logScale(ev));
        if (v > ml) ml = v;
      }
    }
    return ml;
  }, [evals]);

  const stepX = totalMoves > 1 ? chartW / (totalMoves - 1) : 0;

  const cpToY = useCallback(
    (cp: number) => {
      const scaled = logScale(cp);
      const raw = midY - (scaled / maxLog) * (chartH / 2);
      // Clamp to chart bounds so streaming dots can't escape when their
      // value exceeds the confirmed scale.
      return Math.max(PAD_Y, Math.min(PAD_Y + chartH, raw));
    },
    [maxLog, chartH, midY],
  );

  // Build points array using log scale
  const points = useMemo(() => {
    if (n === 0) return [];
    return evals.map((ev, i) => {
      const x = PAD_X + i * stepX;
      const scaled = ev != null ? logScale(ev) : 0;
      const y = midY - (scaled / maxLog) * (chartH / 2);
      return [x, y] as [number, number];
    });
  }, [evals, n, stepX, chartH, midY, maxLog]);

  // Area path: trace points then close to zero line
  const areaPath = useMemo(() => {
    if (points.length === 0) return "";
    const lineSegments = points
      .map(([x, y], i) => (i === 0 ? `M${x},${y}` : `L${x},${y}`))
      .join(" ");
    const lastX = points[points.length - 1][0];
    const firstX = points[0][0];
    return `${lineSegments} L${lastX},${midY} L${firstX},${midY} Z`;
  }, [points, midY]);

  // Line path (just the eval curve, no fill closure)
  const linePath = useMemo(() => {
    if (points.length === 0) return "";
    return points
      .map(([x, y], i) => (i === 0 ? `M${x},${y}` : `L${x},${y}`))
      .join(" ");
  }, [points]);

  // Streaming eval: dashed line from last confirmed point to streaming point
  const streamingPoint = useMemo(() => {
    if (!streamingEval || n === 0) return null;
    const x = PAD_X + streamingEval.index * stepX;
    const y = cpToY(streamingEval.cp);
    return [x, y] as [number, number];
  }, [streamingEval, n, stepX, cpToY]);

  const dashedPath = useMemo(() => {
    if (!streamingPoint || points.length === 0) return "";
    const lastPoint = points[points.length - 1];
    return `M${lastPoint[0]},${lastPoint[1]} L${streamingPoint[0]},${streamingPoint[1]}`;
  }, [streamingPoint, points]);

  // Hollow circles for pending moves (excluding the one being streamed)
  const pendingCircles = useMemo(() => {
    if (!pendingIndices || pendingIndices.length === 0) return [];
    const streamingIdx = streamingEval?.index ?? -1;
    return pendingIndices
      .filter((i) => i !== streamingIdx)
      .map((i) => ({
        cx: PAD_X + i * stepX,
        cy: midY,
      }));
  }, [pendingIndices, streamingEval, stepX, midY]);

  // X position of the current-move indicator
  const indicatorX = useMemo(() => {
    if (totalMoves === 0) return null;
    const idx = currentIndex ?? n - 1;
    if (idx < 0 || idx >= totalMoves) return null;
    return PAD_X + idx * stepX;
  }, [currentIndex, n, totalMoves, stepX]);

  // Click handler: map clientX → move index
  const handleClick = useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      if (n === 0) return;
      const svg = svgRef.current;
      if (!svg) return;
      const rect = svg.getBoundingClientRect();
      const relX = ((e.clientX - rect.left) / rect.width) * SVG_WIDTH;
      const idx = stepX > 0 ? Math.round((relX - PAD_X) / stepX) : 0;
      const clamped = Math.max(0, Math.min(n - 1, idx));
      onSelectMove(clamped);
    },
    [n, stepX, onSelectMove],
  );

  // Evals are white-perspective: positive = white winning (up)
  const topLabel = playerColor === "black" ? "Ghost" : "You";
  const bottomLabel = playerColor === "black" ? "You" : "Ghost";

  if (n === 0 && (!pendingIndices || pendingIndices.length === 0)) return null;

  // Dynamic vertical position for the eval badge within the y-axis.
  // The y-axis stretches to match the SVG height, so we use the full
  // SVG coordinate space (0 → SVG_HEIGHT) for percentage positioning.
  const evalYPercent = useMemo(() => {
    if (evalCp == null) return null;
    const y = cpToY(evalCp);
    const pct = (y / SVG_HEIGHT) * 100;
    return Math.max(5, Math.min(95, pct));
  }, [evalCp, cpToY]);

  const evalBgColor = useMemo(() => {
    if (evalCp == null || !playerColor) return undefined;
    return evalToColor(evalCp, playerColor);
  }, [evalCp, playerColor]);

  return (
    <div
      className={`analysis-graph${playerColor ? " analysis-graph--with-axis" : ""}`}
    >
      <svg
        ref={svgRef}
        viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`}
        preserveAspectRatio="none"
        onClick={handleClick}
      >
        <defs>
          {/* Clip to positive region (above zero line) */}
          <clipPath id={`${clipId}-pos`}>
            <rect x={0} y={0} width={SVG_WIDTH} height={midY} />
          </clipPath>
          {/* Clip to negative region (below zero line) */}
          <clipPath id={`${clipId}-neg`}>
            <rect x={0} y={midY} width={SVG_WIDTH} height={midY} />
          </clipPath>
        </defs>

        {/* Zero line */}
        <line
          x1={PAD_X}
          y1={midY}
          x2={PAD_X + chartW}
          y2={midY}
          className="analysis-graph__zero-line"
        />

        {/* White (positive) area */}
        <path
          d={areaPath}
          clipPath={`url(#${clipId}-pos)`}
          className="analysis-graph__area-white"
        />

        {/* Black (negative) area */}
        <path
          d={areaPath}
          clipPath={`url(#${clipId}-neg)`}
          className="analysis-graph__area-black"
        />

        {/* Eval curve line */}
        <path d={linePath} className="analysis-graph__line" />

        {/* Dashed line to streaming eval */}
        {dashedPath && (
          <path
            d={dashedPath}
            className="analysis-graph__line analysis-graph__line--streaming"
            strokeDasharray="4 3"
            opacity={0.7}
          />
        )}

        {/* Streaming eval point */}
        {streamingPoint && (
          <circle
            cx={streamingPoint[0]}
            cy={streamingPoint[1]}
            r={3}
            className="analysis-graph__streaming-dot"
          />
        )}

        {/* Hollow circles for pending (queued) moves */}
        {pendingCircles.map((c) => (
          <circle
            key={c.cx}
            cx={c.cx}
            cy={c.cy}
            r={2.5}
            className="analysis-graph__pending-dot"
          />
        ))}

        {/* Classification highlight dots */}
        {highlightedMoves && highlightedMoves.indices.map((i) => {
          const pt = points[i];
          if (!pt) return null;
          return (
            <circle
              key={i}
              cx={pt[0]}
              cy={pt[1]}
              r={6}
              className={`analysis-graph__highlight-dot analysis-graph__highlight-dot--${highlightedMoves.classification}`}
            />
          );
        })}

        {/* Current move indicator */}
        {indicatorX != null && (
          <line
            x1={indicatorX}
            y1={PAD_Y}
            x2={indicatorX}
            y2={PAD_Y + chartH}
            className="analysis-graph__indicator"
          />
        )}
      </svg>
      {playerColor && (
        <div className="analysis-graph__y-axis">
          <div className="analysis-graph__y-label">
            <span>{topLabel}</span>
            <svg className="analysis-graph__y-arrow" viewBox="0 0 10 40">
              <line
                x1="5"
                y1="38"
                x2="5"
                y2="4"
                stroke="currentColor"
                strokeWidth="1.5"
              />
              <polyline
                points="1,8 5,2 9,8"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinejoin="round"
              />
            </svg>
          </div>
          {evalYPercent != null && (
            <div
              className="analysis-graph__y-eval"
              style={{ top: `${evalYPercent}%`, background: evalBgColor }}
            >
              {isCheckmate
              ? "#"
              : formatEval(
                  playerColor === "black" ? -evalCp! : evalCp!,
                )}
            </div>
          )}
          <div className="analysis-graph__y-label">
            <svg className="analysis-graph__y-arrow" viewBox="0 0 10 40">
              <line
                x1="5"
                y1="2"
                x2="5"
                y2="36"
                stroke="currentColor"
                strokeWidth="1.5"
              />
              <polyline
                points="1,32 5,38 9,32"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinejoin="round"
              />
            </svg>
            <span>{bottomLabel}</span>
          </div>
        </div>
      )}
    </div>
  );
};

export default memo(AnalysisGraph);
