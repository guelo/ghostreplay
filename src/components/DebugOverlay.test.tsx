import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react'
import DebugOverlay from './DebugOverlay'
import {
  installConsoleCapture,
  isBodyCaptureEnabled,
  uninstallConsoleCapture,
} from '../utils/debugLog'

let originalWindowFetch: typeof window.fetch | undefined

beforeEach(() => {
  originalWindowFetch = window.fetch
  installConsoleCapture()
})

afterEach(() => {
  cleanup()
  uninstallConsoleCapture()
  if (typeof originalWindowFetch === 'function') {
    window.fetch = originalWindowFetch
  } else {
    delete (window as { fetch?: typeof fetch }).fetch
  }
  window.history.replaceState({}, '', '/')
})

/** Re-install the capture with a stubbed window.fetch, then fire one request. */
async function captureFetch(url: string, init?: ResponseInit): Promise<void> {
  uninstallConsoleCapture()
  window.fetch = vi.fn().mockResolvedValue(new Response('{}', init))
  installConsoleCapture()
  await act(async () => {
    try {
      await window.fetch(url)
    } catch {
      /* failures are still captured */
    }
  })
}

function openOverlay() {
  fireEvent.keyDown(window, { key: 'D', ctrlKey: true, shiftKey: true })
}

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

  it('renders a NET filter chip', () => {
    render(<DebugOverlay />)
    openOverlay()
    expect(screen.getByRole('button', { name: 'net' })).toBeInTheDocument()
  })

  it('shows net entries by default and toggling NET hides them', async () => {
    await captureFetch('http://localhost:8000/api/openings/tree', { status: 200 })
    render(<DebugOverlay />)
    openOverlay()

    // NET is in the default-on filter, so the entry is visible immediately.
    expect(screen.getByText(/\/api\/openings\/tree/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'net' }))
    expect(screen.queryByText(/\/api\/openings\/tree/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'net' }))
    expect(screen.getByText(/\/api\/openings\/tree/)).toBeInTheDocument()
  })

  it('marks a failed net entry with the net-fail modifier class', async () => {
    await captureFetch('http://localhost:8000/api/game/end', { status: 500 })
    render(<DebugOverlay />)
    openOverlay()

    const row = screen.getByText(/\/api\/game\/end/).closest('li')
    expect(row).toHaveClass('debug-entry--net')
    expect(row).toHaveClass('debug-entry--net-fail')
  })

  it('renders a Bodies toggle that flips body capture', () => {
    render(<DebugOverlay />)
    openOverlay()

    const btn = screen.getByRole('button', { name: 'Bodies' })
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    expect(isBodyCaptureEnabled()).toBe(false)

    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-pressed', 'true')
    expect(isBodyCaptureEnabled()).toBe(true)

    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    expect(isBodyCaptureEnabled()).toBe(false)
  })
})
