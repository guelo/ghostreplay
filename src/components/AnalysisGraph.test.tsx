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
    expect(tooltip).toHaveTextContent(/win chance/i)
    expect(tooltip).toHaveTextContent(/jump to a move/i)

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

  it('labels the axis directions from the board orientation', async () => {
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
    // White orientation: "You" on top, "Ghost" on the bottom.
    let tooltip = screen.getByRole('tooltip')
    expect(tooltip.querySelector('strong')).toHaveTextContent('You')
    expect(tooltip).toHaveTextContent(/rises\s+toward\s+You/i)
    expect(tooltip).toHaveTextContent(/falls\s+toward\s+Ghost/i)

    rerender(
      <AnalysisGraph
        evals={[0, 50]}
        currentIndex={1}
        onSelectMove={onSelectMove}
        playerColor="black"
      />,
    )
    // Black orientation flips the labels.
    tooltip = screen.getByRole('tooltip')
    expect(tooltip).toHaveTextContent(/rises\s+toward\s+Ghost/i)
    expect(tooltip).toHaveTextContent(/falls\s+toward\s+You/i)
  })
})
