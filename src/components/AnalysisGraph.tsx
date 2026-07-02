import { memo, useCallback, useId, useRef, useMemo } from "react";
import { mateToCp } from "../workers/analysisUtils";
import { formatWhiteEval } from "./MoveRow.helpers";
import { cpToWinningChances } from "./AnalysisGraph.helpers";
import InfoHelpButton from "./InfoHelpButton";

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
  evalMate?: number | null;
  isCheckmate?: boolean;
  streamingEval?: { index: number; cp: number } | null;
  pendingIndices?: number[];
  highlightedMoves?: HighlightedMoves | null;
  variationLine?: VariationLine | null;
};

type VariationLine = {
  anchor: { index: number; cp: number } | null;
  points: { index: number; cp: number; pending: boolean }[];
  streaming: { index: number; cp: number } | null;
};

const SVG_WIDTH = 600;
const SVG_HEIGHT = 120;
const PAD_X = 8;
const PAD_X_RIGHT = 0;
const PAD_Y = 4;
const WINNING_CHANCES_RANGE = 1.05;

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

  return `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})`;
}

const AnalysisGraph = ({
  evals,
  currentIndex,
  onSelectMove,
  playerColor,
  evalCp,
  evalMate,
  isCheckmate,
  streamingEval,
  pendingIndices,
  highlightedMoves,
  variationLine,
}: AnalysisGraphProps) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const clipId = useId();

  const n = evals.length;
  // Total moves includes pending ones for x-axis spacing
  const totalMoves = useMemo(() => {
    let max = n;
    if (pendingIndices && pendingIndices.length > 0) {
      max = Math.max(max, Math.max(...pendingIndices) + 1);
    }
    if (variationLine) {
      const varIndices: number[] = [];
      if (variationLine.anchor) varIndices.push(variationLine.anchor.index);
      for (const p of variationLine.points) varIndices.push(p.index);
      if (variationLine.streaming) varIndices.push(variationLine.streaming.index);
      if (varIndices.length > 0) {
        max = Math.max(max, Math.max(...varIndices) + 1);
      }
    }
    return max;
  }, [n, pendingIndices, variationLine]);

  const chartW = SVG_WIDTH - PAD_X - PAD_X_RIGHT;
  const chartH = SVG_HEIGHT - PAD_Y * 2;
  const midY = PAD_Y + chartH / 2;

  const stepX = totalMoves > 1 ? chartW / (totalMoves - 1) : 0;

  const cpToY = useCallback(
    (cp: number) => {
      const raw = midY - (cpToWinningChances(cp) / WINNING_CHANCES_RANGE) * (chartH / 2);
      // Keep all points inside the chart bounds even for extreme mate scores.
      return Math.max(PAD_Y, Math.min(PAD_Y + chartH, raw));
    },
    [chartH, midY],
  );

  // Build points array using the winning-chances scale.
  const points = useMemo(() => {
    if (n === 0) return [];
    return evals.map((ev, i) => {
      const x = PAD_X + i * stepX;
      const y = cpToY(ev ?? 0);
      return [x, y] as [number, number];
    });
  }, [evals, n, stepX, cpToY]);

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

  // What-if (variation) overlay geometry: dashed polyline + pending/streaming dots
  const variationGeometry = useMemo(() => {
    if (!variationLine) return null;
    const toPoint = (index: number, cp: number): [number, number] => [
      PAD_X + index * stepX,
      cpToY(cp),
    ];
    const linePts: [number, number][] = [];
    if (variationLine.anchor) {
      linePts.push(toPoint(variationLine.anchor.index, variationLine.anchor.cp));
    }
    for (const p of variationLine.points) {
      linePts.push(toPoint(p.index, p.cp));
    }
    const pendingDots = variationLine.points
      .filter((p) => p.pending)
      .map((p) => {
        const [cx, cy] = toPoint(p.index, p.cp);
        return { cx, cy };
      });
    const streamingPt = variationLine.streaming
      ? toPoint(variationLine.streaming.index, variationLine.streaming.cp)
      : null;
    const path =
      linePts.length > 0
        ? linePts
            .map(([x, y], i) => (i === 0 ? `M${x},${y}` : `L${x},${y}`))
            .join(" ")
        : "";
    const streamingDash =
      streamingPt && linePts.length > 0
        ? `M${linePts[linePts.length - 1][0]},${linePts[linePts.length - 1][1]} L${streamingPt[0]},${streamingPt[1]}`
        : "";
    // A single-vertex polyline ("M..." only) does not paint. When the line has
    // just one resolved point and nothing extends it, draw a dot so it shows.
    const soloDot =
      linePts.length === 1 && !streamingDash && pendingDots.length === 0
        ? { cx: linePts[0][0], cy: linePts[0][1] }
        : null;
    return { path, pendingDots, streamingPt, streamingDash, soloDot };
  }, [variationLine, stepX, cpToY]);

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

  const hasVariation =
    !!variationLine &&
    (variationLine.anchor != null ||
      variationLine.points.length > 0 ||
      variationLine.streaming != null);

  // Dynamic vertical position for the eval badge within the y-axis.
  // The y-axis stretches to match the SVG height, so we use the full
  // SVG coordinate space (0 → SVG_HEIGHT) for percentage positioning.
  // For positioning/coloring, fall back to a mate-derived cp when only a mate
  // score is available (e.g. mate-only cached variation analysis) so the badge
  // still renders. The mate code itself takes precedence for the label.
  const badgeCp = evalCp ?? (evalMate != null ? mateToCp(evalMate) : null);

  const evalYPercent = useMemo(() => {
    if (badgeCp == null) return null;
    const y = cpToY(badgeCp);
    const pct = (y / SVG_HEIGHT) * 100;
    return Math.max(5, Math.min(95, pct));
  }, [badgeCp, cpToY]);

  const evalBgColor = useMemo(() => {
    if (badgeCp == null || !playerColor) return undefined;
    return evalToColor(badgeCp, playerColor);
  }, [badgeCp, playerColor]);

  // Hooks must run unconditionally, so this early return comes after them.
  if (
    n === 0 &&
    (!pendingIndices || pendingIndices.length === 0) &&
    !hasVariation
  )
    return null;

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

        {/* What-if (variation) overlay */}
        {variationGeometry && variationGeometry.path && (
          <path
            d={variationGeometry.path}
            className="analysis-graph__line analysis-graph__line--variation"
            strokeDasharray="5 3"
          />
        )}
        {variationGeometry && variationGeometry.streamingDash && (
          <path
            d={variationGeometry.streamingDash}
            className="analysis-graph__line analysis-graph__line--variation"
            strokeDasharray="4 3"
            opacity={0.7}
          />
        )}
        {variationGeometry?.streamingPt && (
          <circle
            cx={variationGeometry.streamingPt[0]}
            cy={variationGeometry.streamingPt[1]}
            r={3}
            className="analysis-graph__streaming-dot analysis-graph__streaming-dot--variation"
          />
        )}
        {variationGeometry?.pendingDots.map((c) => (
          <circle
            key={`var-${c.cx}`}
            cx={c.cx}
            cy={c.cy}
            r={2.5}
            className="analysis-graph__pending-dot analysis-graph__pending-dot--variation"
          />
        ))}
        {variationGeometry?.soloDot && (
          <circle
            cx={variationGeometry.soloDot.cx}
            cy={variationGeometry.soloDot.cy}
            r={3}
            className="analysis-graph__streaming-dot--variation"
          />
        )}

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
              : formatWhiteEval(
                  evalCp != null
                    ? (playerColor === "black" ? -evalCp : evalCp)
                    : null,
                  evalMate != null
                    ? (playerColor === "black" ? -evalMate : evalMate)
                    : null,
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
      <InfoHelpButton
        ariaLabel="What does the evaluation graph show?"
        className="analysis-graph__info"
        popupClassName="info-help-popup--right"
      >
        <p>
          Position evaluation after each move.
        </p>
        <p>
          Click the graph to jump to a move — the red line marks the current
          one.
        </p>
      </InfoHelpButton>
    </div>
  );
};

export default memo(AnalysisGraph);
