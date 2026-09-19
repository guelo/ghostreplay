import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import DebugOverlay from './DebugOverlay'
import {
  installConsoleCapture,
  getNetworkCaptureMode,
  getEntries,
  type NetworkCaptureMode,
  uninstallConsoleCapture,
} from '../utils/debugLog'

let originalWindowFetch: typeof window.fetch | undefined

beforeEach(() => {
  originalWindowFetch = window.fetch
  window.fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}')))
  installConsoleCapture()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  uninstallConsoleCapture()
  if (typeof originalWindowFetch === 'function') {
    window.fetch = originalWindowFetch
  } else {
    delete (window as { fetch?: typeof fetch }).fetch
  }
  window.history.replaceState({}, '', '/')
})

/** Configure fetch before mounting, then fire one request. */
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

  it('defaults to responses and changing mode controls the next fetch', async () => {
    uninstallConsoleCapture()
    window.fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response('{"reply":"d5"}')))
    installConsoleCapture()
    render(<DebugOverlay />)
    openOverlay()
    const select = screen.getByRole('combobox', { name: 'Network capture' })
    expect(select).toHaveValue('responses')

    for (const mode of ['metadata', 'bodies', 'responses'] as const) {
      fireEvent.change(select, { target: { value: mode } })
      expect(getNetworkCaptureMode()).toBe(mode)
      await act(async () => {
        await window.fetch(`/api/${mode}`, { method: 'POST', body: '{"move":"e4"}' })
      })
      if (mode !== 'metadata') {
        await waitFor(() => expect(getEntries().at(-1)?.net?.resBody).toBe('{"reply":"d5"}'))
      }
      const net = getEntries().at(-1)!.net!
      expect(net.reqBody).toBe(mode === 'bodies' ? '{"move":"e4"}' : undefined)
      expect(net.resBody).toBe(mode === 'metadata' ? undefined : '{"reply":"d5"}')
    }
  })

  it.each<NetworkCaptureMode>(['metadata', 'responses', 'bodies'])(
    'reflects stored %s mode, preserving it through Clear and close/reopen', (mode) => {
      uninstallConsoleCapture()
      localStorage.setItem('gr.debugCaptureMode', mode)
      installConsoleCapture()
      console.log('old-history')
      render(<DebugOverlay />)
      openOverlay()
      expect(screen.getByRole('combobox', { name: 'Network capture' })).toHaveValue(mode)
      fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
      expect(getEntries()).toHaveLength(0)
      expect(getNetworkCaptureMode()).toBe(mode)
      openOverlay()
      expect(screen.queryByRole('dialog')).toBeNull()
      openOverlay()
      expect(screen.getByRole('combobox', { name: 'Network capture' })).toHaveValue(mode)
      expect(localStorage.getItem('gr.debugCaptureMode')).toBe(mode)
    },
  )

  it('renders, searches, and copies an asynchronous redacted response captured while closed', async () => {
    let resolveBody!: (body: string) => void
    const body = new Promise<string>((resolve) => { resolveBody = resolve })
    const response = new Response(null)
    vi.spyOn(response, 'clone').mockReturnValue({ body: null, text: () => body } as Response)
    uninstallConsoleCapture()
    window.fetch = vi.fn().mockResolvedValue(response)
    installConsoleCapture()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    render(<DebugOverlay />)
    await act(async () => { await window.fetch('/api/result') })
    openOverlay()
    expect(screen.getByText(/\/api\/result/)).toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('filter…'), { target: { value: 'body-only-match' } })
    expect(screen.queryByText(/\/api\/result/)).toBeNull()

    await act(async () => {
      resolveBody('{"detail":"body-only-match","token":"private-value"}')
      await body
    })
    expect(screen.getByText(/body-only-match/)).toBeInTheDocument()
    expect(screen.queryByText(/private-value/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenLastCalledWith(expect.stringContaining('body-only-match'))
    expect(writeText).toHaveBeenLastCalledWith(expect.stringContaining('[redacted]'))
    expect(writeText.mock.lastCall![0]).not.toContain('private-value')

    // Changing mode keeps captured history; subsequent opt-out bodies stay out of Copy.
    fireEvent.change(screen.getByRole('combobox', { name: 'Network capture' }), {
      target: { value: 'metadata' },
    })
    expect(screen.getByText(/body-only-match/)).toBeInTheDocument()
    await act(async () => { await window.fetch('/api/opt-out') })
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(writeText.mock.lastCall![0]).not.toContain('/api/opt-out')
    fireEvent.change(screen.getByPlaceholderText('filter…'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    const copied = writeText.mock.lastCall![0] as string
    expect(copied).toContain('/api/opt-out')
    expect(copied.match(/body-only-match/g)).toHaveLength(1)
    expect(copied).not.toContain('private-value')
  })

})
