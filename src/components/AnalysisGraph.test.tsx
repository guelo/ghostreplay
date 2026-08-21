import { describe, expect, it, vi } from 'vitest'
import { render, screen, userEvent } from '../test/utils'
import AnalysisGraph from './AnalysisGraph'
import { cpToWinningChances } from './AnalysisGraph.helpers'

const onSelectMove = vi.fn()

/** Helper to extract SVG path `d` attributes by class name. */
function getPathD(container: HTMLElement, className: string) {
  const el = container.querySelector(`.${className}`) as SVGPathElement | null
  return el?.getAttribute('d') ?? null
}

function getLinePoints(container: HTMLElement) {
  const d = getPathD(container, 'analysis-graph__line') ?? ''
  return Array.from(d.matchAll(/[ML]([0-9.]+),([0-9.]+)/g), ([, x, y]) => ({
    x: Number(x),
    y: Number(y),
  }))
}

describe('AnalysisGraph — y-axis', () => {
  it('converts centipawns to Lichess-style winning chances for graph geometry', () => {
    const expected = 2 / (1 + Math.exp(-0.00368208 * 500)) - 1

    expect(cpToWinningChances(0)).toBeCloseTo(0, 8)
    expect(cpToWinningChances(500)).toBeCloseTo(expected, 8)
    expect(cpToWinningChances(-500)).toBeCloseTo(-expected, 8)
    expect(cpToWinningChances(3000)).toBeCloseTo(cpToWinningChances(1000), 8)
  })

  it('renders a mate code (M3) when evalMate is set', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50, 9990]}
        currentIndex={2}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={9990}
        evalMate={3}
      />,
    )

    const evalEl = container.querySelector('.analysis-graph__y-eval')
    expect(evalEl!.textContent).toBe('M3')
  })

  it('renders the mate badge even when evalCp is null (mate-only)', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50, 9990]}
        currentIndex={2}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={null}
        evalMate={2}
      />,
    )

    const evalEl = container.querySelector('.analysis-graph__y-eval') as HTMLElement | null
    expect(evalEl).toBeTruthy()
    expect(evalEl!.textContent).toBe('M2')
    // Positioned high (white winning) via the mate-derived cp fallback
    expect(parseFloat(evalEl!.style.top)).toBeLessThan(50)
  })

  it('inverts the mate code sign for black player', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50, 9990]}
        currentIndex={2}
        onSelectMove={onSelectMove}
        playerColor="black"
        evalCp={9990}
        evalMate={3}
      />,
    )

    const evalEl = container.querySelector('.analysis-graph__y-eval')
    // White mate-in-3 is a loss-in-3 for black
    expect(evalEl!.textContent).toBe('−M3')
  })

  it('renders "#" when isCheckmate is true', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50, 9990]}
        currentIndex={2}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={9990}
        isCheckmate
      />,
    )

    const evalEl = container.querySelector('.analysis-graph__y-eval')
    expect(evalEl).toBeTruthy()
    expect(evalEl!.textContent).toBe('#')
  })

  it('renders "#" at an extreme position for mate-only checkmate (evalCp from mateToCp)', () => {
    // mateToCp(0) = -10000, white perspective on even index = -10000 (black wins)
    const mateEvalCp = -10000
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50, mateEvalCp]}
        currentIndex={2}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={mateEvalCp}
        isCheckmate
      />,
    )

    const evalEl = container.querySelector('.analysis-graph__y-eval') as HTMLElement | null
    expect(evalEl).toBeTruthy()
    expect(evalEl!.textContent).toBe('#')
    // Negative eval (black winning) should be near the bottom (> 50%)
    const top = parseFloat(evalEl!.style.top)
    expect(top).toBeGreaterThan(50)
  })

  it('renders numeric eval when isCheckmate is false', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={150}
      />,
    )

    const evalEl = container.querySelector('.analysis-graph__y-eval')
    expect(evalEl).toBeTruthy()
    expect(evalEl!.textContent).toBe('+1.5')
  })

  it('positions eval badge dynamically via top style', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 200]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={200}
      />,
    )

    const evalEl = container.querySelector('.analysis-graph__y-eval') as HTMLElement | null
    expect(evalEl).toBeTruthy()
    const top = evalEl!.style.top
    expect(top).toMatch(/^\d+(\.\d+)?%$/)
    // +200cp should be above center (< 50%)
    expect(parseFloat(top)).toBeLessThan(50)
  })

  it('y-axis appears after svg (right side)', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={50}
      />,
    )

    const graphEl = container.querySelector('.analysis-graph--with-axis')
    expect(graphEl).toBeTruthy()
    const children = Array.from(graphEl!.children)
    expect(children[0].tagName).toBe('svg')
    expect(children[1].classList.contains('analysis-graph__y-axis')).toBe(true)
  })
})

describe('AnalysisGraph — eval badge color', () => {
  function getBadgeBg(container: HTMLElement) {
    const el = container.querySelector('.analysis-graph__y-eval') as HTMLElement | null
    return el?.style.backgroundColor ?? null
  }

  it('shows green when white is winning as white player', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 500]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={500}
      />,
    )
    expect(getBadgeBg(container)).toBe('rgb(0, 200, 83)')
  })

  it('shows red when white is losing as white player', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, -500]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={-500}
      />,
    )
    expect(getBadgeBg(container)).toBe('rgb(255, 59, 48)')
  })

  it('shows gray at equal eval', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 0]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={0}
      />,
    )
    expect(getBadgeBg(container)).toBe('rgb(158, 158, 158)')
  })

  it('inverts color for black player (positive eval = losing)', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 300]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="black"
        evalCp={300}
      />,
    )
    // +300 white perspective means black is losing → reddish
    const bg = getBadgeBg(container)!
    // Extract red channel — should be > 158 (gray midpoint)
    const r = parseInt(bg.match(/rgba?\((\d+)/)![1])
    expect(r).toBeGreaterThan(158)
  })

  it('inverts label sign for black player', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 150]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="black"
        evalCp={150}
      />,
    )
    const el = container.querySelector('.analysis-graph__y-eval')
    // White +1.5 shown as −1.5 (unicode minus) from black perspective
    expect(el!.textContent).toBe('−1.5')
  })

  it('clamps color at eval beyond +5 pawns', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 1500]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={1500}
      />,
    )
    // Should be clamped to pure winning green
    expect(getBadgeBg(container)).toBe('rgb(0, 200, 83)')
  })

  it('clamps color at eval beyond -5 pawns', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, -1500]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
        evalCp={-1500}
      />,
    )
    // Should be clamped to pure losing red
    expect(getBadgeBg(container)).toBe('rgb(255, 59, 48)')
  })
})

describe('AnalysisGraph — incremental geometry', () => {
  const baseEvals = [0, 50, -30, 120, -80]

  it('confirmed paths stay unchanged across streaming eval updates', () => {
    const { container, rerender } = render(
      <AnalysisGraph
        evals={baseEvals}
        currentIndex={4}
        onSelectMove={onSelectMove}
        streamingEval={{ index: 5, cp: 200 }}
      />,
    )

    const lineBefore = getPathD(container, 'analysis-graph__line')
    const areaWhiteBefore = getPathD(container, 'analysis-graph__area-white')
    const areaBlackBefore = getPathD(container, 'analysis-graph__area-black')

    expect(lineBefore).toBeTruthy()
    expect(areaWhiteBefore).toBeTruthy()

    // Simulate a streaming tick with a very different cp value
    rerender(
      <AnalysisGraph
        evals={baseEvals}
        currentIndex={4}
        onSelectMove={onSelectMove}
        streamingEval={{ index: 5, cp: 900 }}
      />,
    )

    expect(getPathD(container, 'analysis-graph__line')).toBe(lineBefore)
    expect(getPathD(container, 'analysis-graph__area-white')).toBe(areaWhiteBefore)
    expect(getPathD(container, 'analysis-graph__area-black')).toBe(areaBlackBefore)

    // Another tick — negative extreme
    rerender(
      <AnalysisGraph
        evals={baseEvals}
        currentIndex={4}
        onSelectMove={onSelectMove}
        streamingEval={{ index: 5, cp: -1500 }}
      />,
    )

    expect(getPathD(container, 'analysis-graph__line')).toBe(lineBefore)
    expect(getPathD(container, 'analysis-graph__area-white')).toBe(areaWhiteBefore)
    expect(getPathD(container, 'analysis-graph__area-black')).toBe(areaBlackBefore)
  })

  it('appending a resolved eval still updates the path geometry', () => {
    const { container, rerender } = render(
      <AnalysisGraph
        evals={baseEvals}
        currentIndex={4}
        onSelectMove={onSelectMove}
        streamingEval={{ index: 5, cp: 500 }}
      />,
    )

    const lineBefore = getPathD(container, 'analysis-graph__line')

    // Analysis resolves with one extra move, so x-spacing changes.
    const updatedEvals = [...baseEvals, 500]
    rerender(
      <AnalysisGraph
        evals={updatedEvals}
        currentIndex={5}
        onSelectMove={onSelectMove}
        streamingEval={null}
      />,
    )

    const lineAfter = getPathD(container, 'analysis-graph__line')
    expect(lineAfter).toBeTruthy()
    expect(lineAfter).not.toBe(lineBefore)
  })

  it('keeps earlier points fixed when a later eval becomes extreme', () => {
    const { container, rerender } = render(
      <AnalysisGraph
        evals={[50, 100, 150]}
        currentIndex={2}
        onSelectMove={onSelectMove}
      />,
    )

    const before = getLinePoints(container)
    expect(before).toHaveLength(3)

    rerender(
      <AnalysisGraph
        evals={[50, 100, 3000]}
        currentIndex={2}
        onSelectMove={onSelectMove}
      />,
    )

    const after = getLinePoints(container)
    expect(after).toHaveLength(3)

    expect(after[0]).toEqual(before[0])
    expect(after[1]).toEqual(before[1])
    expect(after[2].x).toBe(before[2].x)
    expect(after[2].y).not.toBe(before[2].y)
  })

  it('keeps large late-game eval swings visually separated', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 500, 1000]}
        currentIndex={2}
        onSelectMove={onSelectMove}
      />,
    )

    const points = getLinePoints(container)
    expect(points).toHaveLength(3)

    const yDelta = Math.abs(points[1].y - points[2].y)
    expect(yDelta).toBeGreaterThan(10)
  })

  it('clamps graph geometry beyond the Lichess-style winning-chances cap', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 1000, 3000]}
        currentIndex={2}
        onSelectMove={onSelectMove}
      />,
    )

    const points = getLinePoints(container)
    expect(points).toHaveLength(3)
    expect(points[2].y).toBe(points[1].y)
  })

  it('streaming dot is clamped within chart bounds', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        streamingEval={{ index: 2, cp: 50000 }}
      />,
    )

    const dot = container.querySelector('.analysis-graph__streaming-dot') as SVGCircleElement | null
    expect(dot).toBeTruthy()

    const cy = Number(dot!.getAttribute('cy'))
    // PAD_Y = 4, SVG_HEIGHT = 120, PAD_Y + chartH = 4 + 112 = 116
    expect(cy).toBeGreaterThanOrEqual(4)
    expect(cy).toBeLessThanOrEqual(116)
  })
})

describe('AnalysisGraph — missing evals (null holes)', () => {
  // midY = PAD_Y + chartH/2 = 4 + (120 - 8)/2 = 60 — the equal line.
  const MID_Y = 60

  it('does not plant a trailing null eval on the equal line', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[300, null]}
        currentIndex={1}
        onSelectMove={onSelectMove}
      />,
    )

    const points = getLinePoints(container)
    // Only the resolved point is plotted; the trailing null is skipped, not
    // drawn at midY.
    expect(points).toHaveLength(1)
    expect(points.every((p) => p.y !== MID_Y)).toBe(true)
    // A lone vertex paints no line, so it must show an explicit dot instead.
    const dots = container.querySelectorAll('.analysis-graph__line-dot')
    expect(dots).toHaveLength(1)
    expect(Number((dots[0] as SVGCircleElement).getAttribute('cy'))).not.toBe(MID_Y)
  })

  it('renders a visible dot for a lone eval that would otherwise paint nothing', () => {
    // Sparse series — only the synthesized terminal checkmate is non-null (every
    // earlier ply still pending). Its one-point run emits just an SVG moveto (no
    // stroke) and a zero-width area, so without a dot the point is invisible.
    const { container } = render(
      <AnalysisGraph
        evals={[null, null, 10000]}
        currentIndex={2}
        onSelectMove={onSelectMove}
      />,
    )

    const dots = container.querySelectorAll('.analysis-graph__line-dot')
    expect(dots).toHaveLength(1)
    const cy = Number((dots[0] as SVGCircleElement).getAttribute('cy'))
    expect(cy).not.toBe(MID_Y)
    expect(cy).toBeLessThan(MID_Y) // pegged high = white winning, not the equal line
  })

  it('anchors the streaming dash to the last non-null point, not a trailing null', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[300, null]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        streamingEval={{ index: 2, cp: 200 }}
      />,
    )

    const dashed = getPathD(container, 'analysis-graph__line--streaming') ?? ''
    // Dash must start from the resolved vertex (index 0), never from a null hole.
    expect(dashed).toMatch(/^M[0-9.]+,[0-9.]+ L/)
    const startY = Number(dashed.match(/^M[0-9.]+,([0-9.]+)/)?.[1])
    expect(startY).not.toBe(MID_Y)
    // The streaming dash already makes that vertex visible, so no redundant dot.
    expect(container.querySelectorAll('.analysis-graph__line-dot')).toHaveLength(0)
  })

  it('breaks the line and area into separate subpaths across an interior null', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[300, 200, null, -200, -300]}
        currentIndex={4}
        onSelectMove={onSelectMove}
      />,
    )

    const line = getPathD(container, 'analysis-graph__line') ?? ''
    // Two runs → two `M` move-to commands (the line is broken, not interpolated).
    expect((line.match(/M/g) ?? []).length).toBe(2)

    const points = getLinePoints(container)
    expect(points).toHaveLength(4)
    // No vertex parked on the equal line despite the interior gap.
    expect(points.every((p) => p.y !== MID_Y)).toBe(true)

    // The filled area is split into one closed run per contiguous segment.
    const area = getPathD(container, 'analysis-graph__area-white') ?? ''
    expect((area.match(/Z/g) ?? []).length).toBe(2)

    // Both runs have length >= 2 and paint a line, so no isolated-point dots.
    expect(container.querySelectorAll('.analysis-graph__line-dot')).toHaveLength(0)
  })

  it('renders no isolated dots for a fully contiguous series', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50, -30, 120]}
        currentIndex={3}
        onSelectMove={onSelectMove}
      />,
    )

    expect(container.querySelectorAll('.analysis-graph__line-dot')).toHaveLength(0)
  })
})

describe('AnalysisGraph — variation (what-if) overlay', () => {
  it('renders the dashed variation polyline and pending dots', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50, -20]}
        currentIndex={3}
        onSelectMove={onSelectMove}
        variationLine={{
          anchor: { index: 1, cp: 50 },
          points: [
            { index: 2, cp: 120, pending: false },
            { index: 3, cp: 0, pending: true },
          ],
          streaming: null,
        }}
      />,
    )

    const line = container.querySelector('.analysis-graph__line--variation') as SVGPathElement | null
    expect(line).toBeTruthy()
    // anchor + 2 points = 3 vertices
    const verts = (line!.getAttribute('d') ?? '').match(/[ML]/g) ?? []
    expect(verts.length).toBe(3)

    // One pending dot for the unanalysed ply
    const pendingDots = container.querySelectorAll('.analysis-graph__pending-dot--variation')
    expect(pendingDots.length).toBe(1)
  })

  it('renders a dashed streaming segment + live dot when streaming present', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50]}
        currentIndex={2}
        onSelectMove={onSelectMove}
        variationLine={{
          anchor: { index: 1, cp: 50 },
          points: [{ index: 2, cp: 0, pending: false }],
          streaming: { index: 3, cp: 200 },
        }}
      />,
    )

    const dot = container.querySelector('.analysis-graph__streaming-dot--variation')
    expect(dot).toBeTruthy()
    // Two variation paths: the polyline and the streaming dash
    const varPaths = container.querySelectorAll('.analysis-graph__line--variation')
    expect(varPaths.length).toBe(2)
  })

  it('still renders a start-position what-if line when there are no main moves', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[]}
        currentIndex={0}
        onSelectMove={onSelectMove}
        variationLine={{
          anchor: null,
          points: [{ index: 0, cp: 30, pending: false }],
          streaming: null,
        }}
      />,
    )

    expect(container.querySelector('svg')).toBeTruthy()
    // A lone resolved point can't paint as a polyline, so a visible dot is drawn.
    expect(container.querySelector('.analysis-graph__streaming-dot--variation')).toBeTruthy()
  })

  it('omits the anchor when the branch departs the starting position', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 50]}
        currentIndex={0}
        onSelectMove={onSelectMove}
        variationLine={{
          anchor: null,
          points: [{ index: 0, cp: 30, pending: false }],
          streaming: null,
        }}
      />,
    )

    const line = container.querySelector('.analysis-graph__line--variation') as SVGPathElement | null
    expect(line).toBeTruthy()
    const verts = (line!.getAttribute('d') ?? '').match(/[ML]/g) ?? []
    expect(verts.length).toBe(1)
  })
})

describe('AnalysisGraph — classification highlight dots', () => {
  it('renders one colored dot per classified move, skipping null-eval holes', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, null, 50, -30]}
        currentIndex={3}
        onSelectMove={onSelectMove}
        playerColor="white"
        highlightedMoves={{
          dots: [
            { index: 0, classification: 'blunder' },
            { index: 1, classification: 'mistake' },
            { index: 2, classification: 'inaccuracy' },
          ],
        }}
      />,
    )

    // index 1 has a null eval (points[1] === null) so its dot is skipped.
    const dots = container.querySelectorAll('.analysis-graph__highlight-dot')
    expect(dots).toHaveLength(2)
    expect(container.querySelector('.analysis-graph__highlight-dot--blunder')).toBeTruthy()
    expect(container.querySelector('.analysis-graph__highlight-dot--inaccuracy')).toBeTruthy()
    // The skipped null-eval dot was the only mistake — no mistake dot renders.
    expect(container.querySelector('.analysis-graph__highlight-dot--mistake')).toBeNull()
  })
})

describe('AnalysisGraph — opening boundary', () => {
  it('places the shaded band and boundary between opening and middlegame plies', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 20, 40, 10, -10, 5]}
        currentIndex={5}
        onSelectMove={onSelectMove}
        openingPlyCount={3}
      />,
    )

    const band = container.querySelector('.analysis-graph__opening-band')
    const boundary = container.querySelector('.analysis-graph__opening-boundary')
    const expectedX = 8 + (3 - 0.5) * (592 / 5)
    expect(Number(band?.getAttribute('width'))).toBeCloseTo(expectedX)
    expect(Number(boundary?.getAttribute('x1'))).toBeCloseTo(expectedX)
    expect(boundary?.getAttribute('x1')).toBe(boundary?.getAttribute('x2'))
  })

  it('keeps the band behind the areas and the full-height boundary above them', () => {
    const { container } = render(
      <AnalysisGraph
        evals={[0, 20, 40, 10, -10, 5]}
        currentIndex={5}
        onSelectMove={onSelectMove}
        openingPlyCount={3}
      />,
    )

    const svg = container.querySelector('.analysis-graph > svg')!
    const children = Array.from(svg.children)
    const indexOf = (selector: string) =>
      children.indexOf(svg.querySelector(selector) as Element)
    const bandIndex = indexOf('.analysis-graph__opening-band')
    const whiteAreaIndex = indexOf('.analysis-graph__area-white')
    const blackAreaIndex = indexOf('.analysis-graph__area-black')
    const boundaryIndex = indexOf('.analysis-graph__opening-boundary')
    const curveIndex = indexOf('.analysis-graph__line')
    const indicatorIndex = indexOf('.analysis-graph__indicator')

    expect(bandIndex).toBeLessThan(whiteAreaIndex)
    expect(bandIndex).toBeLessThan(blackAreaIndex)
    expect(boundaryIndex).toBeGreaterThan(whiteAreaIndex)
    expect(boundaryIndex).toBeGreaterThan(blackAreaIndex)
    expect(boundaryIndex).toBeLessThan(curveIndex)
    expect(indicatorIndex).toBe(children.length - 1)
  })

  it.each([undefined, null, 0, 5])(
    'renders no opening decoration for an unavailable or out-of-domain boundary (%s)',
    (openingPlies) => {
      const { container } = render(
        <AnalysisGraph
          evals={[0, 20, 40, 10]}
          currentIndex={3}
          onSelectMove={onSelectMove}
          openingPlyCount={openingPlies}
        />,
      )

      expect(container.querySelector('.analysis-graph__opening-band')).toBeNull()
      expect(container.querySelector('.analysis-graph__opening-boundary')).toBeNull()
      expect(container.querySelector('.analysis-graph__opening-label')).toBeNull()
    },
  )

  it('renders an undistorted overlay label and flips it after an early boundary', () => {
    const { container } = render(
      <AnalysisGraph
        evals={Array(20).fill(0)}
        currentIndex={19}
        onSelectMove={onSelectMove}
        openingPlyCount={1}
      />,
    )

    const label = screen.getByText('opening')
    expect(label).toHaveClass('analysis-graph__opening-label--after')
    expect(label).not.toBe(container.querySelector('svg text'))
  })

  it('shifts the label clear of the help button at the right-edge clamp', () => {
    render(
      <AnalysisGraph
        evals={Array(17).fill(0)}
        currentIndex={16}
        onSelectMove={onSelectMove}
        openingPlyCount={17}
      />,
    )

    expect(screen.getByText('opening')).toHaveClass(
      'analysis-graph__opening-label--before-help',
    )
  })

  it('keeps the label attached to the boundary before the help-button guard', () => {
    render(
      <AnalysisGraph
        evals={Array(20).fill(0)}
        currentIndex={19}
        onSelectMove={onSelectMove}
        openingPlyCount={18}
      />,
    )

    expect(screen.getByText('opening')).not.toHaveClass(
      'analysis-graph__opening-label--before-help',
    )
  })
})

describe('AnalysisGraph — info button', () => {
  it('toggles an explanatory popup when the info button is clicked', async () => {
    const user = userEvent.setup()
    onSelectMove.mockClear()
    render(
      <AnalysisGraph
        evals={[0, 50, -30]}
        currentIndex={2}
        onSelectMove={onSelectMove}
        playerColor="white"
      />,
    )

    const btn = screen.getByRole('button', {
      name: /what does the evaluation graph show/i,
    })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    await user.click(btn)
    const tooltip = screen.getByRole('tooltip')
    expect(tooltip).toHaveTextContent(/position evaluation after each move/i)
    expect(tooltip).toHaveTextContent(/jump to a move/i)
    expect(tooltip).toHaveTextContent(/red line marks the current/i)
    expect(tooltip).toHaveTextContent(/shaded opening ends where the lichess phase divider/i)

    // Clicking the info button must not navigate the graph.
    expect(onSelectMove).not.toHaveBeenCalled()

    await user.click(btn)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('closes the popup when clicking outside', async () => {
    const user = userEvent.setup()
    render(
      <AnalysisGraph
        evals={[0, 50]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
      />,
    )

    await user.click(
      screen.getByRole('button', { name: /what does the evaluation graph show/i }),
    )
    expect(screen.getByRole('tooltip')).toBeInTheDocument()

    await user.click(document.body)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('keeps the popup explanation stable when the board orientation changes', async () => {
    const user = userEvent.setup()
    const { rerender } = render(
      <AnalysisGraph
        evals={[0, 50]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="white"
      />,
    )

    await user.click(
      screen.getByRole('button', { name: /what does the evaluation graph show/i }),
    )
    let tooltip = screen.getByRole('tooltip')
    expect(tooltip).toHaveTextContent(/position evaluation after each move/i)
    expect(tooltip).toHaveTextContent(/jump to a move/i)

    rerender(
      <AnalysisGraph
        evals={[0, 50]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="black"
      />,
    )
    tooltip = screen.getByRole('tooltip')
    expect(tooltip).toHaveTextContent(/position evaluation after each move/i)
    expect(tooltip).toHaveTextContent(/jump to a move/i)
  })
})
