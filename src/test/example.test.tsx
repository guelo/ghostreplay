import { describe, it, expect } from 'vitest'
import { render, screen } from './utils'

describe('Test Setup', () => {
  it('should render a basic component', () => {
    const TestComponent = () => <div>Hello Test</div>

    render(<TestComponent />)

    expect(screen.getByText('Hello Test')).toBeInTheDocument()
  })
})
