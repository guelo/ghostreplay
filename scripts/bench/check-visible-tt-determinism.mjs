#!/usr/bin/env node

import { resolve } from 'node:path'
import { runnerImport } from 'vite'

const { module } = await runnerImport(
  resolve('src/bench/node/visibleTtDeterminism.ts'),
  { logLevel: 'error' },
)
await module.runVisibleTtDeterminismCli()
