import { useCallback, useId, useRef, useMemo } from "react";

type AnalysisGraphProps = {
  evals: (number | null)[];
  currentIndex: number | null;
  onSelectMove: (index: number) => void;
  playerColor?: "white" | "black";
  evalCp?: number | null;
};

const SVG_WIDTH = 600;
const SVG_HEIGHT = 120;
const PAD_X = 8;
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
}: AnalysisGraphProps) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const clipId = useId();

  const n = evals.length;
  const chartW = SVG_WIDTH - PAD_X * 2;
  const chartH = SVG_HEIGHT - PAD_Y * 2;
  const midY = PAD_Y + chartH / 2;

  // Build points array using log scale
  const points = useMemo(() => {
    if (n === 0) return [];
    // Find the max log-scaled value to normalize the y-axis
    let maxLog = logScale(200); // minimum range ≈ ±2 pawns
    for (const ev of evals) {
      if (ev != null) {
        const v = Math.abs(logScale(ev));
        if (v > maxLog) maxLog = v;
      }
    }
    const stepX = n > 1 ? chartW / (n - 1) : 0;
    return evals.map((ev, i) => {
      const x = PAD_X + i * stepX;
      const scaled = ev != null ? logScale(ev) : 0;
      // positive eval → above center (lower y), negative → below center
      const y = midY - (scaled / maxLog) * (chartH / 2);
      return [x, y] as [number, number];
    });
  }, [evals, n, chartW, chartH, midY]);

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

  // X position of the current-move indicator
  const indicatorX = useMemo(() => {
    if (n === 0) return null;
    const idx = currentIndex ?? n - 1;
    if (idx < 0 || idx >= n) return null;
    const stepX = n > 1 ? chartW / (n - 1) : 0;
    return PAD_X + idx * stepX;
  }, [currentIndex, n, chartW]);

  // Click handler: map clientX → move index
  const handleClick = useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      if (n === 0) return;
      const svg = svgRef.current;
      if (!svg) return;
      const rect = svg.getBoundingClientRect();
      const relX = ((e.clientX - rect.left) / rect.width) * SVG_WIDTH;
      const stepX = n > 1 ? chartW / (n - 1) : 0;
      const idx = stepX > 0 ? Math.round((relX - PAD_X) / stepX) : 0;
      const clamped = Math.max(0, Math.min(n - 1, idx));
      onSelectMove(clamped);
    },
    [n, chartW, onSelectMove],
  );

  // Evals are white-perspective: positive = white winning (up)
  const topLabel = playerColor === "black" ? "Ghost" : "You";
  const bottomLabel = playerColor === "black" ? "You" : "Ghost";

  if (n === 0) return null;

  return (
    <div
      className={`analysis-graph${playerColor ? " analysis-graph--with-axis" : ""}`}
    >
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
          {evalCp != null && (
            <div className="analysis-graph__y-eval">{formatEval(evalCp)}</div>
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
    </div>
  );
};

export default AnalysisGraph;
