import { defineConfig } from 'vitest/config'

/**
 * Node-side tooling tests, separate from the app suite.
 *
 * `vitest.config.ts` runs in jsdom, aliases `fs`/`path` to browser stubs and
 * loads a setup file that touches `HTMLElement.prototype` at import time. None
 * of that survives in a script that has to open real files and bind real
 * sockets, and bending the app config to accommodate one directory would risk
 * the whole frontend suite. A second config costs a few lines and cannot.
 */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['scripts/**/*.test.mjs'],
  },
})
