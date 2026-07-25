import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  // `npm run lint` is `eslint .`, so eslint walks the entire repo and every
  // tree that is not a lint target has to be pruned here. Two reasons, both
  // real:
  //   - Races. This repo is edited by several agents at once and AGENTS.md
  //     tells all of them to run pytest with TMPDIR=backend/.tmp, so backend/
  //     (and .beads/, which bd rewrites) gain and lose whole subtrees while
  //     eslint is mid-walk. eslint readdir's an entry that is gone by the time
  //     it scandir's it and aborts the whole run with ENOENT, flaking the
  //     pre-push "Frontend lint" gate (g-eslint-skip-backend).
  //   - Cost. backend/ and .beads/ alone are ~1.2GB eslint can never lint.
  // None of these trees contain a .ts/.tsx file. Add a tree here when it starts
  // churning, never to silence a lint error in real source.
  globalIgnores([
    'dist',
    'backend',
    '.beads',
    'coverage',
    'tmp',
    '.test-results',
    'test-results',
    'blob-report',
    'playwright-report',
    'e2e/screenshots/output',
  ]),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },
  {
    files: ['**/*.test.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
])
