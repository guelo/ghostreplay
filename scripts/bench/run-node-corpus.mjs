#!/usr/bin/env node

import { resolve } from 'node:path'
import { runnerImport } from 'vite'

const { module } = await runnerImport(
  resolve('src/bench/node/runCorpus.ts'),
  { logLevel: 'error' },
)
await module.runCorpusCli(process.argv.slice(2))
