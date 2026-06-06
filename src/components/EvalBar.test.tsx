import { describe, expect, it } from 'vitest'
import { render, screen } from '../test/utils'
import EvalBar from './EvalBar'

describe('EvalBar', () => {
  it('displays centipawn eval text', () => {
    render(
      <EvalBar
        whitePerspectiveCp={120}
        whitePerspectiveMate={null}
        whiteOnBottom
      />,
    )

    expect(screen.getByText('+1.2')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Evaluation +1.2' })).toBeInTheDocument()
  })

  it('displays mate eval text', () => {
    render(
      <EvalBar
        whitePerspectiveCp={null}
        whitePerspectiveMate={3}
        whiteOnBottom
      />,
    )

    expect(screen.getByText('M3')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Evaluation M3' })).toBeInTheDocument()
  })

  it('renders "#" for a checkmate (mate 0) and fills toward the winner', () => {
    const { container } = render(
      <EvalBar
        whitePerspectiveCp={9990}
        whitePerspectiveMate={0}
        whiteOnBottom
      />,
    )

    expect(screen.getByText('#')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Evaluation #' })).toBeInTheDocument()
    const fill = container.querySelector('.eval-bar__white-fill') as HTMLElement
    // White delivered mate (positive cp) → bar fills strongly toward white
    expect(parseFloat(fill.style.height)).toBeGreaterThan(95)
  })

  it('renders "#" filled toward black when black delivered mate', () => {
    const { container } = render(
      <EvalBar
        whitePerspectiveCp={-9990}
        whitePerspectiveMate={0}
        whiteOnBottom
      />,
    )

    expect(screen.getByText('#')).toBeInTheDocument()
    const fill = container.querySelector('.eval-bar__white-fill') as HTMLElement
    expect(parseFloat(fill.style.height)).toBeLessThan(5)
  })
})
