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
})
