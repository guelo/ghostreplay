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

describe('MoveList eval formulas', () => {
  it('shows the full eval formula when white improves after a white move', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 30 },
    ])
    expect(deltas[0]).toBe('0 +0.3 = +0.3')
  })

  it('shows the full eval formula when white improves after a bad black move', () => {
    const deltas = renderAndGetDeltas([
      { san: 'd4', eval: 30 },
      { san: 'h5', eval: 160 },
    ])
    expect(deltas[1]).toBe('+0.3 +1.3 = +1.6')
  })

  it('shows the full eval formula when eval drops for white', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 100 },
      { san: 'e5', eval: 50 },
      { san: 'Qh5', eval: -100 },
    ])
    expect(deltas[2]).toBe('+0.5 −1.5 = −1')
  })

  it('shows the full eval formula when eval does not change', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 30 },
      { san: 'e5', eval: 30 },
    ])
    expect(deltas[1]).toBe('+0.3 +0 = +0.3')
  })

  it('rounds values less than 5cp to 0 in the displayed formula', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 0 },
      { san: 'e5', eval: 4 },
    ])
    expect(deltas[1]).toBe('0 +0.0 = +0.0')
  })

  it('shows the rounded delta for 5cp or more', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 0 },
      { san: 'e5', eval: 50 },
    ])
    expect(deltas[1]).toBe('0 +0.5 = +0.5')
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
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 50 },
    ])
    expect(deltas[0]).toBe('0 +0.5 = +0.5')
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
