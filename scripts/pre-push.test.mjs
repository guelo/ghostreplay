import { execFileSync, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, expect, it } from 'vitest'

const hook = fileURLToPath(new URL('../.githooks/pre-push', import.meta.url))
let scratch
let env

beforeEach(() => {
  scratch = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pre-push-')))
  // The harness must be safe even when this test itself runs inside a Git hook.
  env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('GIT_')))
  Object.assign(env, { GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1' })
})

afterEach(() => {
  fs.rmSync(scratch, { recursive: true, force: true })
})

function git(cwd, ...args) {
  return execFileSync('git', args, { cwd, env, encoding: 'utf8', timeout: 10_000 }).trim()
}

function snapshot(main, linked) {
  return {
    refs: git(main, 'show-ref'),
    config: fs.readFileSync(path.join(main, '.git/config')),
    trees: [main, linked].map((tree) => {
      const admin = git(tree, 'rev-parse', '--absolute-git-dir')
      return {
        head: fs.readFileSync(path.join(admin, 'HEAD')),
        index: fs.readFileSync(path.join(admin, 'index')),
        config: fs.readFileSync(path.join(admin, 'config.worktree')),
      }
    }),
  }
}

function setup() {
  const main = path.join(scratch, 'main checkout')
  const linked = path.join(scratch, 'linked checkout')
  const remote = path.join(scratch, 'remote.git')
  git(scratch, 'init', '-q', '--initial-branch=main', main)
  git(main, 'config', 'user.name', 'Parent Author')
  git(main, 'config', 'user.email', 'parent@example.invalid')
  git(main, 'config', 'commit.gpgsign', 'false')
  fs.writeFileSync(path.join(main, 'seed'), 'seed\n')
  git(main, 'add', 'seed')
  git(main, 'commit', '-qm', 'parent seed')
  git(main, 'worktree', 'add', '-q', '--detach', linked)
  git(scratch, 'init', '-q', '--bare', remote)
  git(main, 'config', 'extensions.worktreeConfig', 'true')
  for (const tree of [main, linked]) {
    git(tree, 'config', '--worktree', 'user.name', `${path.basename(tree)} Author`)
    // Keep staged changes so an accidentally redirected fixture commit is observable.
    fs.writeFileSync(path.join(tree, 'staged'), 'preserve staged contents\n')
    git(tree, 'add', 'staged')
    fs.mkdirSync(path.join(tree, '.githooks'))
    fs.copyFileSync(hook, path.join(tree, '.githooks/pre-push'))
    fs.chmodSync(path.join(tree, '.githooks/pre-push'), 0o755)
    fs.mkdirSync(path.join(tree, 'backend/.venv/bin'), { recursive: true })
  }
  git(main, 'config', 'core.hooksPath', '.githooks')

  // Run the real hook and real Git operations, with cheap stand-ins for each
  // gate command. Running the full test suites here would recurse into this test.
  const probe = path.join(scratch, 'gate-probe.cjs')
  fs.writeFileSync(probe, `#!/usr/bin/env node
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const args = process.argv.slice(2)
const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8' }).trim()
const localVars = git(process.cwd(), 'rev-parse', '--local-env-vars').split('\\n')
const leaked = localVars.filter(key => Object.hasOwn(process.env, key))
const fixture = fs.mkdtempSync(path.join(process.env.GATE_FIXTURES, 'fixture-'))
git(fixture, 'init', '-q')
git(fixture, 'config', 'user.name', 'Fixture Author')
git(fixture, 'config', 'user.email', 'fixture@example.invalid')
fs.writeFileSync(path.join(fixture, 'seed'), 'fixture seed\\n')
git(fixture, 'add', 'seed')
git(fixture, 'commit', '-qm', 'fixture seed')
fs.appendFileSync(process.env.GATE_RECORD, JSON.stringify({
  args, leaked, fixture, cwd: process.cwd(),
  root: git(fixture, 'rev-parse', '--show-toplevel'),
  author: git(fixture, 'log', '-1', '--format=%an <%ae>'),
  transport: process.env.GIT_SSH_COMMAND,
}) + '\\n')
if (process.env.GATE_FAILURE === 'exit' && args.includes('lint')) process.exit(7)
if (process.env.GATE_FAILURE === 'warning' && args.includes('build')) console.log('Warning: fixture warning')
`)
  fs.chmodSync(probe, 0o755)
  const bin = path.join(scratch, 'bin')
  fs.mkdirSync(bin)
  fs.symlinkSync(probe, path.join(bin, 'npm'))
  for (const tree of [main, linked]) {
    fs.symlinkSync(probe, path.join(tree, 'backend/.venv/bin/python'))
  }
  const fixtures = path.join(scratch, 'fixtures')
  fs.mkdirSync(fixtures)
  const record = path.join(scratch, 'gates.jsonl')
  const gateEnv = {
    ...env,
    PATH: `${bin}${path.delimiter}${env.PATH}`,
    GATE_FIXTURES: fixtures,
    GATE_RECORD: record,
    // Transport settings are not repository-local and must survive the hook.
    GIT_SSH_COMMAND: 'ssh -oBatchMode=yes',
  }
  return { main, linked, remote, record, gateEnv }
}

it.each([
  { checkout: 'main', failure: '' },
  { checkout: 'linked', failure: '' },
  { checkout: 'linked', failure: 'exit' },
  { checkout: 'linked', failure: 'warning' },
])('isolates fixture Git commands during a $checkout push ($failure)', ({ checkout, failure }) => {
  const { main, linked, remote, record, gateEnv } = setup()
  const tree = checkout === 'main' ? main : linked
  const before = snapshot(main, linked)
  const result = spawnSync('git', ['push', remote, 'HEAD:refs/heads/test'], {
    cwd: tree,
    env: { ...gateEnv, GATE_FAILURE: failure },
    encoding: 'utf8',
    timeout: 30_000,
  })
  expect(result.error).toBeUndefined()
  expect(snapshot(main, linked)).toEqual(before)
  expect(result.status, result.stdout + result.stderr).toBe(failure ? 1 : 0)
  const records = fs.readFileSync(record, 'utf8').trim().split('\n').map(JSON.parse)
  expect(records.map(({ args }) => args)).toEqual([
    ['run', 'lint', '--', '--max-warnings=0'],
    ['run', 'build'],
    ['run', 'test:run', '--', '--no-color'],
    ['run', 'test:scripts:run', '--', '--no-color'],
    ['run', 'test:e2e:run'],
    ['-m', 'ruff', 'check', '--config', 'backend/ruff.toml', '--no-cache', 'backend'],
    ['-W', 'error', '-m', 'pytest', '-c', 'backend/pytest.ini', 'backend', '-m', 'not release_seal'],
  ])
  for (const row of records) {
    expect(row.leaked).toEqual([])
    expect(row.cwd).toBe(tree)
    expect(row.root).toBe(row.fixture)
    expect(row.author).toBe('Fixture Author <fixture@example.invalid>')
    expect(row.transport).toBe(gateEnv.GIT_SSH_COMMAND)
  }
  expect(git(scratch, '--git-dir', remote, 'for-each-ref', '--format=%(objectname)')).toBe(
    failure ? '' : git(tree, 'rev-parse', 'HEAD'),
  )
}, 30_000)

it('clears explicit repository paths and config overrides before running any gate', () => {
  const { main, linked, record, gateEnv } = setup()
  const admin = git(linked, 'rev-parse', '--absolute-git-dir')
  const common = path.join(main, '.git')
  const before = snapshot(main, linked)
  const result = spawnSync('bash', [path.join(linked, '.githooks/pre-push')], {
    // Root discovery must precede clearing the variables: this is not the checkout.
    cwd: scratch,
    env: {
      ...gateEnv,
      GIT_DIR: admin,
      GIT_COMMON_DIR: common,
      GIT_WORK_TREE: linked,
      GIT_INDEX_FILE: path.join(admin, 'index'),
      GIT_OBJECT_DIRECTORY: path.join(common, 'objects'),
      GIT_ALTERNATE_OBJECT_DIRECTORIES: path.join(common, 'objects'),
      GIT_CONFIG_COUNT: '1',
      GIT_CONFIG_KEY_0: 'user.name',
      GIT_CONFIG_VALUE_0: 'Inherited Author',
      GIT_CONFIG_PARAMETERS: "'user.email=inherited@example.invalid'",
    },
    encoding: 'utf8',
    timeout: 30_000,
  })
  expect(result.error).toBeUndefined()
  expect(snapshot(main, linked)).toEqual(before)
  expect(result.status, result.stdout + result.stderr).toBe(0)
  const records = fs.readFileSync(record, 'utf8').trim().split('\n').map(JSON.parse)
  expect(records).toHaveLength(7)
  for (const row of records) {
    expect(row.leaked).toEqual([])
    expect(row.cwd).toBe(linked)
    expect(row.root).toBe(row.fixture)
    expect(row.author).toBe('Fixture Author <fixture@example.invalid>')
  }
}, 30_000)
