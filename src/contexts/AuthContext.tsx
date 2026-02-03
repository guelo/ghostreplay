import {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  type ReactNode,
} from 'react'

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const STORAGE_KEYS = {
  credentials: 'ghost_replay_credentials',
  token: 'ghost_replay_token',
} as const

interface Credentials {
  username: string
  password: string
}

interface User {
  id: number
  username: string
  isAnonymous: boolean
}

interface AuthState {
  user: User | null
  token: string | null
  isLoading: boolean
  error: string | null
}

interface AuthContextValue extends AuthState {
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  claimAccount: (email: string, password: string) => Promise<void>
}

interface AuthResponse {
  token: string
  user_id: number
  username: string
}

const AuthContext = createContext<AuthContextValue | null>(null)

/**
 * Generate a random string for anonymous credentials
 */
const generateRandomString = (length: number): string => {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789'
  const array = new Uint8Array(length)
  crypto.getRandomValues(array)
  return Array.from(array, (byte) => chars[byte % chars.length]).join('')
}

/**
 * Generate anonymous credentials for auto-registration
 */
const generateCredentials = (): Credentials => ({
  username: `ghost_${generateRandomString(8)}`,
  password: generateRandomString(24),
})

/**
 * Get stored credentials from localStorage
 */
const getStoredCredentials = (): Credentials | null => {
  const stored = localStorage.getItem(STORAGE_KEYS.credentials)
  if (!stored) return null
  try {
    return JSON.parse(stored) as Credentials
  } catch {
    return null
  }
}

/**
 * Store credentials in localStorage
 */
const storeCredentials = (credentials: Credentials): void => {
  localStorage.setItem(STORAGE_KEYS.credentials, JSON.stringify(credentials))
}

/**
 * Call the login API
 */
const apiLogin = async (
  username: string,
  password: string
): Promise<AuthResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })

  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(error.detail || 'Login failed')
  }

  return response.json()
}

/**
 * Call the register API
 */
const apiRegister = async (
  username: string,
  password: string
): Promise<AuthResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })

  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(error.detail || 'Registration failed')
  }

  return response.json()
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    token: null,
    isLoading: true,
    error: null,
  })

  /**
   * Initialize auth on mount:
   * 1. Check localStorage for credentials
   * 2. If found, login with them
   * 3. If not found, generate new credentials and register
   */
  useEffect(() => {
    const initAuth = async () => {
      const storedCredentials = getStoredCredentials()

      if (storedCredentials) {
        // Try to login with stored credentials
        try {
          const response = await apiLogin(
            storedCredentials.username,
            storedCredentials.password
          )
          localStorage.setItem(STORAGE_KEYS.token, response.token)
          setState({
            user: {
              id: response.user_id,
              username: response.username,
              isAnonymous: true,
            },
            token: response.token,
            isLoading: false,
            error: null,
          })
          return
        } catch {
          // Stored credentials invalid, generate new ones
          localStorage.removeItem(STORAGE_KEYS.credentials)
        }
      }

      // No valid credentials, register new anonymous account
      const newCredentials = generateCredentials()
      try {
        const response = await apiRegister(
          newCredentials.username,
          newCredentials.password
        )
        storeCredentials(newCredentials)
        localStorage.setItem(STORAGE_KEYS.token, response.token)
        setState({
          user: {
            id: response.user_id,
            username: response.username,
            isAnonymous: true,
          },
          token: response.token,
          isLoading: false,
          error: null,
        })
      } catch (err) {
        setState({
          user: null,
          token: null,
          isLoading: false,
          error: err instanceof Error ? err.message : 'Authentication failed',
        })
      }
    }

    initAuth()
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    setState((prev) => ({ ...prev, isLoading: true, error: null }))
    try {
      const response = await apiLogin(username, password)
      storeCredentials({ username, password })
      localStorage.setItem(STORAGE_KEYS.token, response.token)
      setState({
        user: {
          id: response.user_id,
          username: response.username,
          isAnonymous: false,
        },
        token: response.token,
        isLoading: false,
        error: null,
      })
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isLoading: false,
        error: err instanceof Error ? err.message : 'Login failed',
      }))
      throw err
    }
  }, [])

  const logout = useCallback(() => {
    localStorage.removeItem(STORAGE_KEYS.credentials)
    localStorage.removeItem(STORAGE_KEYS.token)
    // Generate new anonymous credentials
    const newCredentials = generateCredentials()
    apiRegister(newCredentials.username, newCredentials.password)
      .then((response) => {
        storeCredentials(newCredentials)
        localStorage.setItem(STORAGE_KEYS.token, response.token)
        setState({
          user: {
            id: response.user_id,
            username: response.username,
            isAnonymous: true,
          },
          token: response.token,
          isLoading: false,
          error: null,
        })
      })
      .catch(() => {
        setState({
          user: null,
          token: null,
          isLoading: false,
          error: null,
        })
      })
  }, [])

  const claimAccount = useCallback(
    async (_email: string, _password: string) => {
      // TODO: Implement account claim endpoint
      throw new Error('Account claim not yet implemented')
    },
    []
  )

  return (
    <AuthContext.Provider value={{ ...state, login, logout, claimAccount }}>
      {children}
    </AuthContext.Provider>
  )
}

/**
 * Hook to access auth context
 */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
