import { describe, expect, it, vi } from 'vitest'
import { render } from '../test/utils'
import MoveList from './MoveList'

// jsdom doesn't implement scrollIntoView
Element.prototype.scrollIntoView = vi.fn()

const noop = () => {}

/**
 * Render MoveList and return the delta text for each move cell.
 */
function renderAndGetDeltas(
  moves: Array<{
    san: string
    eval?: number | null
    classification?: null
  }>,
  currentIndex: number | null = null,
) {
  const { container } = render(
    <MoveList moves={moves} currentIndex={currentIndex} onNavigate={noop} />,
  )
  const evalSpans = container.querySelectorAll('.move-eval')
  return Array.from(evalSpans).map((el) => el.textContent ?? '')
}

function renderAndGetHeaderEval(
  moves: Array<{
    san: string
    eval?: number | null
    classification?: null
  }>,
  currentIndex: number | null = null,
) {
  const { container } = render(
    <MoveList moves={moves} currentIndex={currentIndex} onNavigate={noop} />,
  )
  const header = container.querySelector('.move-list-header-eval')
  return header?.textContent ?? ''
}

describe('MoveList delta arrows', () => {
  it('shows ↑ when eval improves for white after a white move', () => {
    // 1. e4: eval goes from 0 (start) to +0.3
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 30 },
    ])
    expect(deltas[0]).toContain('↑')
    expect(deltas[0]).toContain('0.3')
  })

  it('shows ↑ when eval improves for white after a bad black move', () => {
    // 1. d4 h5: eval goes from +0.3 to +1.6
    // From white's perspective eval went up by 1.3 → ↑1.3
    const deltas = renderAndGetDeltas([
      { san: 'd4', eval: 30 },
      { san: 'h5', eval: 160 },
    ])
    expect(deltas[1]).toContain('↑')
    expect(deltas[1]).toContain('1.3')
  })

  it('shows ↓ when eval drops for white', () => {
    // White blunders: eval goes from +1.0 to -0.5
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 100 },
      { san: 'e5', eval: 50 },
      { san: 'Qh5', eval: -100 },
    ])
    expect(deltas[2]).toContain('↓')
    expect(deltas[2]).toContain('1.5')
  })

  it('shows =0 when eval does not change', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 30 },
      { san: 'e5', eval: 30 },
    ])
    expect(deltas[1]).toBe('=0')
  })

  it('rounds values less than 5cp to 0', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 0 },
      { san: 'e5', eval: 4 },
    ])
    expect(deltas[1]).toBe('=0')
  })

  it('shows arrow for 5cp or more', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 0 },
      { san: 'e5', eval: 50 },
    ])
    expect(deltas[1]).toContain('0.5')
  })

  it('shows nothing when eval is not available', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4' },
    ])
    expect(deltas[0]).toBe('')
  })

  it('shows nothing when previous eval is not available', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4' },
      { san: 'e5', eval: 50 },
    ])
    expect(deltas[1]).toBe('')
  })

  it('first move uses 0 as baseline (starting position)', () => {
    // First move: eval goes from 0 (implicit start) to +0.5
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 50 },
    ])
    expect(deltas[0]).toContain('↑')
    expect(deltas[0]).toContain('0.5')
  })
})

describe('MoveList header eval', () => {
  it('shows eval of selected move in the header', () => {
    const headerEval = renderAndGetHeaderEval(
      [
        { san: 'e4', eval: 30 },
        { san: 'e5', eval: 50 },
      ],
      0,
    )
    expect(headerEval).toBe('+0.3')
  })

  it('shows eval of last move when currentIndex is null', () => {
    const headerEval = renderAndGetHeaderEval(
      [
        { san: 'e4', eval: 30 },
        { san: 'e5', eval: 50 },
      ],
      null,
    )
    expect(headerEval).toBe('+0.5')
  })

  it('shows nothing when no eval is available', () => {
    const headerEval = renderAndGetHeaderEval(
      [{ san: 'e4' }],
      0,
    )
    expect(headerEval).toBe('')
  })
})
