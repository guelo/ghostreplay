import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react'
import DebugOverlay from './DebugOverlay'
import { installConsoleCapture, uninstallConsoleCapture } from '../utils/debugLog'

beforeEach(() => {
  installConsoleCapture()
})

afterEach(() => {
  cleanup()
  uninstallConsoleCapture()
  window.history.replaceState({}, '', '/')
})

describe('DebugOverlay', () => {
  it('is hidden by default', () => {
    render(<DebugOverlay />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('toggles open with Ctrl+Shift+D', () => {
    render(<DebugOverlay />)
    fireEvent.keyDown(window, { key: 'D', ctrlKey: true, shiftKey: true })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'D', ctrlKey: true, shiftKey: true })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('auto-opens when ?debug=1 is present', () => {
    window.history.replaceState({}, '', '/?debug=1')
    render(<DebugOverlay />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('renders entries and filters by level and text', () => {
    console.log('alpha message')
    console.error('beta failure')
    render(<DebugOverlay />)
    fireEvent.keyDown(window, { key: 'D', ctrlKey: true, shiftKey: true })

    expect(screen.getByText('alpha message')).toBeInTheDocument()
    expect(screen.getByText('beta failure')).toBeInTheDocument()

    // Disable the "log" level chip -> alpha hidden.
    fireEvent.click(screen.getByRole('button', { name: 'log' }))
    expect(screen.queryByText('alpha message')).toBeNull()
    expect(screen.getByText('beta failure')).toBeInTheDocument()

    // Re-enable, then text filter.
    fireEvent.click(screen.getByRole('button', { name: 'log' }))
    fireEvent.change(screen.getByPlaceholderText('filter…'), {
      target: { value: 'beta' },
    })
    expect(screen.queryByText('alpha message')).toBeNull()
    expect(screen.getByText('beta failure')).toBeInTheDocument()
  })

  it('live-updates for logs captured after mount', () => {
    render(<DebugOverlay />)
    fireEvent.keyDown(window, { key: 'D', ctrlKey: true, shiftKey: true })
    expect(screen.queryByText('live-after-mount')).toBeNull()

    act(() => {
      console.log('live-after-mount')
    })
    expect(screen.getByText('live-after-mount')).toBeInTheDocument()
  })

  it('clears entries', () => {
    console.log('to be cleared')
    render(<DebugOverlay />)
    fireEvent.keyDown(window, { key: 'D', ctrlKey: true, shiftKey: true })
    expect(screen.getByText('to be cleared')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(screen.queryByText('to be cleared')).toBeNull()
  })
})
