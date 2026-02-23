import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from '@playwright/test'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

const frontendPort = Number(process.env.E2E_FRONTEND_PORT ?? 4173)
const backendPort = Number(process.env.E2E_BACKEND_PORT ?? 8010)
const baseURL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${frontendPort}`
const apiURL = process.env.E2E_API_URL ?? `http://127.0.0.1:${backendPort}`

const defaultDbPath = path.resolve(__dirname, 'backend', '.tmp', 'e2e.sqlite3')
const normalizedDbPath = defaultDbPath.split(path.sep).join('/')
const databaseUrl = process.env.E2E_DATABASE_URL ?? `sqlite:///${normalizedDbPath}`

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  fullyParallel: true,
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [['list'], ['html', { open: 'never' }]],
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
      command: `npm run dev -- --host 127.0.0.1 --port ${frontendPort}`,
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
