import { defineConfig } from '@playwright/test'

import {
  apiURL,
  backendPort,
  baseURL,
  databaseUrl,
  frontendPort,
  outputDir,
  reportDir,
} from './e2e/env'

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  fullyParallel: true,
  outputDir,
  // Headroom for a machine running more than one suite. Per-run isolation makes
  // concurrent runs correct (g-e2e-port-collide) but not free — two suites at
  // once roughly doubles wall-clock — and a test that waits on the engine twice
  // needs to stay clear of the ceiling. Costs a passing run nothing.
  timeout: 60_000,
  expect: {
    // Same reasoning as the test timeout above, for the same reason. The most
    // common mode-3 signature in g-e2e-port-collide is a bare
    // `expect(locator).toBeVisible()` giving up after 5000ms on a machine that
    // was merely busy — a build someone had to prove innocent three suite runs
    // later. These specs assert layout and behaviour, not latency (the bench
    // suites in src/bench do that), so waiting longer for a state that does
    // arrive costs nothing and only delays the report on one that never does.
    timeout: 15_000,
  },
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [['list'], ['html', { open: 'never', outputFolder: reportDir }]],
  use: {
    baseURL,
    trace: 'on-first-retry',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
  webServer: [
    {
      command: 'bash scripts/e2e/start_backend.sh',
      url: `${apiURL}/health`,
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        ...process.env,
        DATABASE_URL: databaseUrl,
        E2E_API_URL: apiURL,
        BACKEND_PORT: String(backendPort),
      },
    },
    {
      // --strictPort so a taken port fails immediately. Without it Vite silently
      // moves to the next free port and Playwright then waits out its full 120s
      // timeout on a baseURL nothing is serving (g-e2e-port-collide).
      command: `npm run dev -- --host 127.0.0.1 --strictPort --port ${frontendPort}`,
      url: baseURL,
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        ...process.env,
        VITE_API_URL: apiURL,
      },
    },
  ],
})
