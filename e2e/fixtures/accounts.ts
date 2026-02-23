export type SeedUserRole = 'due' | 'stable' | 'empty'

type SeedUser = {
  username: string
  password: string
}

const valueOrDefault = (key: string, fallback: string): string =>
  process.env[key] ?? fallback

export const seedUsers: Record<SeedUserRole, SeedUser> = {
  due: {
    username: valueOrDefault('E2E_DUE_USERNAME', 'e2e_due_user'),
    password: valueOrDefault('E2E_DUE_PASSWORD', 'e2e-pass-123'),
  },
  stable: {
    username: valueOrDefault('E2E_STABLE_USERNAME', 'e2e_stable_user'),
    password: valueOrDefault('E2E_STABLE_PASSWORD', 'e2e-pass-123'),
  },
  empty: {
    username: valueOrDefault('E2E_EMPTY_USERNAME', 'e2e_empty_user'),
    password: valueOrDefault('E2E_EMPTY_PASSWORD', 'e2e-pass-123'),
  },
}
