import {
  expect,
  test as base,
  type APIRequestContext,
  type APIResponse,
  type Page,
} from '@playwright/test'
import { apiURL as apiBaseURL } from '../env'
import { seedUsers, type SeedUserRole } from './accounts'

type AuthFixtures = {
  loginAs: (page: Page, role: SeedUserRole) => Promise<void>
}

/** Connection-level failures — the request never reached the app. */
const TRANSPORT_ERROR = /ECONNRESET|EPIPE|ECONNREFUSED|socket hang up/i

/**
 * Log in, retrying a dropped connection.
 *
 * Playwright's request context pools sockets, so a login can land in the
 * window between the server closing an idle keep-alive connection and the
 * client noticing — it then fails with ECONNRESET before the test has done
 * anything of its own. scripts/e2e/start_backend.sh raises uvicorn's
 * --timeout-keep-alive above any gap a run produces, which shuts that window;
 * this retry keeps a stray transport hiccup from failing the whole gate over
 * something that is not a product defect. See g-e2e-login-econnreset.
 *
 * Only connection errors are retried. An HTTP error response is a real
 * failure and still fails the test.
 */
const loginRequest = async (
  request: APIRequestContext,
  data: { username: string; password: string },
  attempts = 3,
): Promise<APIResponse> => {
  for (let attempt = 1; ; attempt++) {
    try {
      return await request.post(`${apiBaseURL}/api/auth/login`, { data })
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (attempt >= attempts || !TRANSPORT_ERROR.test(message)) throw error
      // The pooled socket is gone either way; the next attempt opens a fresh one.
      await new Promise((resolve) => setTimeout(resolve, 100 * attempt))
    }
  }
}

export const test = base.extend<AuthFixtures>({
  loginAs: async ({ request }, runFixture) => {
    await runFixture(async (page, role) => {
      const account = seedUsers[role]
      const response = await loginRequest(request, {
        username: account.username,
        password: account.password,
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
