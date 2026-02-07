import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act, cleanup } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import { AuthProvider } from './AuthContext'
import { useAuth } from './useAuth'

/**
 * Build a fake JWT with the given payload (no real signature).
 */
function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))
  const body = btoa(JSON.stringify(payload))
  return `${header}.${body}.fake-signature`
}

const mockAuthResponse = {
  token: 'mock-jwt-token',
  user_id: 123,
  username: 'ghost_abc12345',
}

// Persistent mock storage that survives test lifecycle
let mockLocalStorage: Record<string, string> = {}

const localStorageMock = {
  getItem: (key: string) => mockLocalStorage[key] ?? null,
  setItem: (key: string, value: string) => {
    mockLocalStorage[key] = value
  },
  removeItem: (key: string) => {
    delete mockLocalStorage[key]
  },
  clear: () => {
    mockLocalStorage = {}
  },
  length: 0,
  key: () => null,
}

Object.defineProperty(window, 'localStorage', {
  value: localStorageMock,
  writable: true,
})

describe('AuthContext', () => {
  let mockFetch: ReturnType<typeof vi.fn>

  beforeEach(() => {
    mockLocalStorage = {}
    mockFetch = vi.fn()
    globalThis.fetch = mockFetch as unknown as typeof fetch
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  describe('auto-registration', () => {
    it('registers new anonymous user when no credentials exist', async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockAuthResponse),
      })

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      )

      expect(screen.getByTestId('loading')).toHaveTextContent('true')

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false')
      })

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/auth/register'),
        expect.objectContaining({
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
        })
      )

      expect(screen.getByTestId('user-id')).toHaveTextContent('123')
      expect(screen.getByTestId('is-anonymous')).toHaveTextContent('true')
      expect(mockLocalStorage['ghost_replay_credentials']).toBeDefined()
      expect(mockLocalStorage['ghost_replay_token']).toBe('mock-jwt-token')
    })
  })

  describe('auto-login', () => {
    it('logs in with stored credentials on mount', async () => {
      mockLocalStorage['ghost_replay_credentials'] = JSON.stringify({
        username: 'ghost_existing',
        password: 'existing-password-12345678',
      })

      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockAuthResponse),
      })

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      )

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false')
      })

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/auth/login'),
        expect.objectContaining({ method: 'POST' })
      )

      expect(screen.getByTestId('user-id')).toHaveTextContent('123')
    })

    it('keeps credentials on transient server error', async () => {
      mockLocalStorage['ghost_replay_credentials'] = JSON.stringify({
        username: 'ghost_existing',
        password: 'existing-password-12345678',
      })

      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 500,
        json: () => Promise.resolve({ detail: 'Internal server error' }),
      })

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      )

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false')
      })

      // Should NOT have called register — credentials are preserved
      expect(mockFetch).toHaveBeenCalledTimes(1)
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/auth/login'),
        expect.any(Object)
      )
      // Credentials should still be in localStorage
      expect(mockLocalStorage['ghost_replay_credentials']).toBeDefined()
      expect(screen.getByTestId('error')).toHaveTextContent('Internal server error')
    })

    it('keeps credentials on network error', async () => {
      mockLocalStorage['ghost_replay_credentials'] = JSON.stringify({
        username: 'ghost_existing',
        password: 'existing-password-12345678',
      })

      mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      )

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false')
      })

      expect(mockFetch).toHaveBeenCalledTimes(1)
      expect(mockLocalStorage['ghost_replay_credentials']).toBeDefined()
      expect(screen.getByTestId('error')).toHaveTextContent('Failed to fetch')
    })

    it('re-registers when stored credentials are invalid', async () => {
      mockLocalStorage['ghost_replay_credentials'] = JSON.stringify({
        username: 'ghost_invalid',
        password: 'invalid-password-12345678',
      })

      mockFetch
        .mockResolvedValueOnce({
          ok: false,
          status: 401,
          json: () => Promise.resolve({ detail: 'Invalid credentials' }),
        })
        .mockResolvedValueOnce({
          ok: true,
          json: () => Promise.resolve(mockAuthResponse),
        })

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      )

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false')
      })

      expect(mockFetch).toHaveBeenCalledTimes(2)
      expect(mockFetch).toHaveBeenNthCalledWith(
        1,
        expect.stringContaining('/api/auth/login'),
        expect.any(Object)
      )
      expect(mockFetch).toHaveBeenNthCalledWith(
        2,
        expect.stringContaining('/api/auth/register'),
        expect.any(Object)
      )
    })
  })

  describe('token restoration', () => {
    it('restores claimed user from stored JWT without API call', async () => {
      const token = makeJwt({
        sub: '456',
        username: 'claimed_user',
        is_anonymous: false,
        exp: Math.floor(Date.now() / 1000) + 3600,
      })
      mockLocalStorage['ghost_replay_token'] = token

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      )

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false')
      })

      expect(mockFetch).not.toHaveBeenCalled()
      expect(screen.getByTestId('user-id')).toHaveTextContent('456')
      expect(screen.getByTestId('username')).toHaveTextContent('claimed_user')
      expect(screen.getByTestId('is-anonymous')).toHaveTextContent('false')
      expect(screen.getByTestId('token')).toHaveTextContent(token)
    })

    it('restores anonymous user from stored JWT without API call', async () => {
      const token = makeJwt({
        sub: '123',
        username: 'ghost_abc12345',
        is_anonymous: true,
        exp: Math.floor(Date.now() / 1000) + 3600,
      })
      mockLocalStorage['ghost_replay_token'] = token

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      )

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false')
      })

      expect(mockFetch).not.toHaveBeenCalled()
      expect(screen.getByTestId('user-id')).toHaveTextContent('123')
      expect(screen.getByTestId('is-anonymous')).toHaveTextContent('true')
    })

    it('falls through to credentials when token is expired', async () => {
      const expiredToken = makeJwt({
        sub: '456',
        username: 'claimed_user',
        is_anonymous: false,
        exp: Math.floor(Date.now() / 1000) - 60,
      })
      mockLocalStorage['ghost_replay_token'] = expiredToken
      mockLocalStorage['ghost_replay_credentials'] = JSON.stringify({
        username: 'claimed_user',
        password: 'my-secure-password-123',
      })

      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockAuthResponse),
      })

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      )

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false')
      })

      // Should have fallen through to credential-based login
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/auth/login'),
        expect.objectContaining({ method: 'POST' })
      )
      // Expired token should have been cleared
      expect(mockLocalStorage['ghost_replay_token']).toBe('mock-jwt-token')
    })
  })

  describe('login', () => {
    it('updates state and stores credentials on successful login', async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockAuthResponse),
      })

      const { result } = renderHook(() => useAuth(), {
        wrapper: AuthProvider,
      })

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false)
      })

      const loginResponse = {
        token: 'new-token',
        user_id: 456,
        username: 'claimed_user',
      }
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(loginResponse),
      })

      await act(async () => {
        await result.current.login('claimed_user', 'my-secure-password')
      })

      expect(result.current.user?.id).toBe(456)
      expect(result.current.user?.isAnonymous).toBe(false)
      expect(result.current.token).toBe('new-token')
    })

    it('throws on failed login', async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockAuthResponse),
      })

      const { result } = renderHook(() => useAuth(), {
        wrapper: AuthProvider,
      })

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false)
      })

      mockFetch.mockResolvedValue({
        ok: false,
        json: () => Promise.resolve({ detail: 'Invalid credentials' }),
      })

      await expect(
        act(async () => {
          await result.current.login('bad_user', 'bad-password-12345')
        })
      ).rejects.toThrow('Invalid credentials')
    })
  })

  describe('logout', () => {
    it('clears credentials and registers new anonymous user', async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockAuthResponse),
      })

      const { result } = renderHook(() => useAuth(), {
        wrapper: AuthProvider,
      })

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false)
      })

      const newAnonResponse = {
        token: 'new-anon-token',
        user_id: 789,
        username: 'ghost_newanon',
      }
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(newAnonResponse),
      })

      act(() => {
        result.current.logout()
      })

      await waitFor(() => {
        expect(result.current.user?.id).toBe(789)
      })

      expect(result.current.user?.isAnonymous).toBe(true)
      expect(result.current.token).toBe('new-anon-token')
    })
  })

  describe('useAuth hook', () => {
    it('throws when used outside AuthProvider', () => {
      expect(() => {
        renderHook(() => useAuth())
      }).toThrow('useAuth must be used within an AuthProvider')
    })
  })
})

function TestConsumer() {
  const { user, token, isLoading, error } = useAuth()

  return (
    <div>
      <span data-testid="loading">{String(isLoading)}</span>
      <span data-testid="user-id">{user?.id ?? 'null'}</span>
      <span data-testid="username">{user?.username ?? 'null'}</span>
      <span data-testid="is-anonymous">{String(user?.isAnonymous ?? 'null')}</span>
      <span data-testid="token">{token ?? 'null'}</span>
      <span data-testid="error">{error ?? 'null'}</span>
    </div>
  )
}
