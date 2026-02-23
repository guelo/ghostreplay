import { expect, test as base, type Page } from '@playwright/test'
import { seedUsers, type SeedUserRole } from './accounts'

type AuthFixtures = {
  loginAs: (page: Page, role: SeedUserRole) => Promise<void>
}

const apiBaseURL = process.env.E2E_API_URL ?? 'http://127.0.0.1:8010'

export const test = base.extend<AuthFixtures>({
  loginAs: async ({ request }, runFixture) => {
    await runFixture(async (page, role) => {
      const account = seedUsers[role]
      const response = await request.post(`${apiBaseURL}/api/auth/login`, {
        data: {
          username: account.username,
          password: account.password,
        },
      })
      expect(response.ok()).toBeTruthy()

      const payload = (await response.json()) as { token: string }
      await page.addInitScript(
        ({ token, credentials }) => {
          localStorage.setItem('ghost_replay_token', token)
          localStorage.setItem(
            'ghost_replay_credentials',
            JSON.stringify(credentials),
          )
        },
        {
          token: payload.token,
          credentials: {
            username: account.username,
            password: account.password,
          },
        },
      )
    })
  },
})

export { expect }
