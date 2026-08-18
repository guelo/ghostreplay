export const AUTH_STORAGE_KEYS = {
  credentials: 'ghost_replay_credentials',
  token: 'ghost_replay_token',
} as const

export const hasNoStoredAuthIdentity = (): boolean => {
  if (typeof window === 'undefined') {
    return false
  }

  try {
    return (
      window.localStorage.getItem(AUTH_STORAGE_KEYS.token) === null &&
      window.localStorage.getItem(AUTH_STORAGE_KEYS.credentials) === null
    )
  } catch {
    // Storage can be unavailable in hardened browser contexts. Without a
    // reliable absence signal, keep the returning-user classification path.
    return false
  }
}
