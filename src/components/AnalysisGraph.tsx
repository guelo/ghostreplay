import { useCallback, useRef, useMemo } from 'react'

type AnalysisGraphProps = {
  evals: (number | null)[]
  currentIndex: number | null
  onSelectMove: (index: number) => void
}

const CLAMP = 500 // ±5 pawns in centipawns
const SVG_WIDTH = 600
const SVG_HEIGHT = 120
const PAD_X = 8
const PAD_Y = 4

const clamp = (v: number) => Math.max(-CLAMP, Math.min(CLAMP, v))

const AnalysisGraph = ({
  evals,
  currentIndex,
  onSelectMove,
}: AnalysisGraphProps) => {
  const svgRef = useRef<SVGSVGElement>(null)

  const n = evals.length
  const chartW = SVG_WIDTH - PAD_X * 2
  const chartH = SVG_HEIGHT - PAD_Y * 2
  const midY = PAD_Y + chartH / 2

  // Build points array: [x, y] for each eval
  const points = useMemo(() => {
    if (n === 0) return []
    const stepX = n > 1 ? chartW / (n - 1) : 0
    return evals.map((ev, i) => {
      const x = PAD_X + i * stepX
      const cp = ev != null ? clamp(ev) : 0
      // positive eval → above center (lower y), negative → below center
      const y = midY - (cp / CLAMP) * (chartH / 2)
      return [x, y] as [number, number]
    })
  }, [evals, n, chartW, chartH, midY])

  // Area path: trace points then close to zero line
  const areaPath = useMemo(() => {
    if (points.length === 0) return ''
    const lineSegments = points
      .map(([x, y], i) => (i === 0 ? `M${x},${y}` : `L${x},${y}`))
      .join(' ')
    const lastX = points[points.length - 1][0]
    const firstX = points[0][0]
    return `${lineSegments} L${lastX},${midY} L${firstX},${midY} Z`
  }, [points, midY])

  // Line path (just the eval curve, no fill closure)
  const linePath = useMemo(() => {
    if (points.length === 0) return ''
    return points
      .map(([x, y], i) => (i === 0 ? `M${x},${y}` : `L${x},${y}`))
      .join(' ')
  }, [points])

  // X position of the current-move indicator
  const indicatorX = useMemo(() => {
    if (n === 0) return null
    const idx = currentIndex ?? n - 1
    if (idx < 0 || idx >= n) return null
    const stepX = n > 1 ? chartW / (n - 1) : 0
    return PAD_X + idx * stepX
  }, [currentIndex, n, chartW])

  // Click handler: map clientX → move index
  const handleClick = useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      if (n === 0) return
      const svg = svgRef.current
      if (!svg) return
      const rect = svg.getBoundingClientRect()
      const relX = ((e.clientX - rect.left) / rect.width) * SVG_WIDTH
      const stepX = n > 1 ? chartW / (n - 1) : 0
      const idx = stepX > 0 ? Math.round((relX - PAD_X) / stepX) : 0
      const clamped = Math.max(0, Math.min(n - 1, idx))
      onSelectMove(clamped)
    },
    [n, chartW, onSelectMove],
  )

  if (n === 0) return null

  return (
    <div className="analysis-graph">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`}
        preserveAspectRatio="none"
        onClick={handleClick}
      >
        <defs>
          {/* Clip to positive region (above zero line) */}
          <clipPath id="clip-positive">
            <rect x={0} y={0} width={SVG_WIDTH} height={midY} />
          </clipPath>
          {/* Clip to negative region (below zero line) */}
          <clipPath id="clip-negative">
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
          clipPath="url(#clip-positive)"
          className="analysis-graph__area-white"
        />

        {/* Black (negative) area */}
        <path
          d={areaPath}
          clipPath="url(#clip-negative)"
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
  )
}

export default AnalysisGraph
