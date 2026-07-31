import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Chess } from 'chess.js'
import corpusJson from './corpus.json'

export const CORPUS_SCHEMA_VERSION = 1
export const MIN_CORPUS_POSITIONS = 200

export type CorpusPhase = 'opening' | 'middlegame' | 'endgame'

export const CORPUS_TAGS = [
  'quiet',
  'tactical',
  'target-best',
  'target-excellent',
  'target-good',
  'target-inaccuracy',
  'target-mistake',
  'target-blunder',
  'win-chance-02pct',
  'win-chance-10pct',
  'win-chance-20pct',
  'win-chance-30pct',
  'recording-50cp',
  'drill-0cp',
  'drill-10cp',
  'drill-25cp',
  'drill-50cp',
  'blunder-boundary-30pct',
  'mate-winning',
  'mate-losing',
  'mate-changing',
  'mate-in-1',
  'checkmate',
  'stalemate',
  'insufficient-material',
  'single-legal-move',
  'forced-mate-below-depth',
  'depth-best-change',
  'regression-1e4',
  'regression-g-kgiq',
] as const

export type CorpusTag = (typeof CORPUS_TAGS)[number]

export type CorpusPosition = {
  id: string
  fen: string
  playedMove: string
  playerColor: 'white' | 'black'
  phase: CorpusPhase
  tags: CorpusTag[]
  label: string
  source: string
}

export type CorpusFile = {
  schemaVersion: number
  description: string
  positions: CorpusPosition[]
}

const TAG_SET = new Set<string>(CORPUS_TAGS)
const PHASES = new Set<string>(['opening', 'middlegame', 'endgame'])
const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbn]?$/

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const parseUci = (uci: string) => ({
  from: uci.slice(0, 2),
  to: uci.slice(2, 4),
  ...(uci.length > 4 ? { promotion: uci.slice(4, 5) } : {}),
})

const validatePosition = (
  value: unknown,
  index: number,
): { row: CorpusPosition | null; problems: string[] } => {
  const where = `positions[${index}]`
  if (!isObject(value)) {
    return { row: null, problems: [`${where} is not an object`] }
  }

  const problems: string[] = []
  const strings = ['id', 'fen', 'playedMove', 'label', 'source'] as const
  for (const field of strings) {
    if (typeof value[field] !== 'string' || value[field].length === 0) {
      problems.push(`${where}.${field} is missing or empty`)
    }
  }
  if (value.playerColor !== 'white' && value.playerColor !== 'black') {
    problems.push(`${where}.playerColor is invalid`)
  }
  if (!PHASES.has(String(value.phase))) {
    problems.push(`${where}.phase is invalid`)
  }
  if (!Array.isArray(value.tags) || value.tags.length === 0) {
    problems.push(`${where}.tags is missing or empty`)
  } else {
    const unknown = value.tags.find((tag) => typeof tag !== 'string' || !TAG_SET.has(tag))
    if (unknown !== undefined) {
      problems.push(`${where}.tags contains unknown tag ${JSON.stringify(unknown)}`)
    }
    if (new Set(value.tags).size !== value.tags.length) {
      problems.push(`${where}.tags contains a duplicate`)
    }
  }

  if (typeof value.playedMove === 'string' && !UCI_MOVE.test(value.playedMove)) {
    problems.push(`${where}.playedMove is not UCI`)
  }

  if (typeof value.fen === 'string' && typeof value.playedMove === 'string') {
    try {
      const chess = new Chess(value.fen)
      const expectedColor = chess.turn() === 'w' ? 'white' : 'black'
      if (value.playerColor !== expectedColor) {
        problems.push(
          `${where}.playerColor ${String(value.playerColor)} disagrees with FEN ${expectedColor}`,
        )
      }
      if (!chess.move(parseUci(value.playedMove))) {
        problems.push(`${where}.playedMove is illegal in its FEN`)
      }
    } catch (error) {
      problems.push(`${where} cannot be replayed: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  return {
    row: problems.length === 0 ? value as CorpusPosition : null,
    problems,
  }
}

const REQUIRED_TAGS: readonly CorpusTag[] = [
  'quiet',
  'tactical',
  'target-best',
  'target-excellent',
  'target-good',
  'target-inaccuracy',
  'target-mistake',
  'target-blunder',
  'win-chance-02pct',
  'win-chance-10pct',
  'win-chance-20pct',
  'win-chance-30pct',
  'recording-50cp',
  'drill-0cp',
  'drill-10cp',
  'drill-25cp',
  'drill-50cp',
  'blunder-boundary-30pct',
  'mate-winning',
  'mate-losing',
  'mate-changing',
  'mate-in-1',
  'checkmate',
  'stalemate',
  'insufficient-material',
  'single-legal-move',
  'forced-mate-below-depth',
  'depth-best-change',
  'regression-1e4',
  'regression-g-kgiq',
]

export const corpusProblems = (value: unknown): string[] => {
  if (!isObject(value)) return ['corpus is not an object']
  const problems: string[] = []
  if (value.schemaVersion !== CORPUS_SCHEMA_VERSION) {
    problems.push(
      `unsupported schemaVersion ${String(value.schemaVersion)} (expected ${CORPUS_SCHEMA_VERSION})`,
    )
  }
  if (typeof value.description !== 'string' || value.description.length === 0) {
    problems.push('description is missing')
  }
  if (!Array.isArray(value.positions)) {
    return [...problems, 'positions is missing']
  }
  if (value.positions.length < MIN_CORPUS_POSITIONS) {
    problems.push(`corpus has ${value.positions.length} positions; need at least ${MIN_CORPUS_POSITIONS}`)
  }

  const rows: CorpusPosition[] = []
  value.positions.forEach((position, index) => {
    const validated = validatePosition(position, index)
    problems.push(...validated.problems)
    if (validated.row) rows.push(validated.row)
  })

  const ids = new Set<string>()
  const positionMoves = new Set<string>()
  for (const row of rows) {
    if (ids.has(row.id)) problems.push(`duplicate position id ${row.id}`)
    ids.add(row.id)
    const key = `${row.fen}|${row.playedMove}`
    if (positionMoves.has(key)) {
      problems.push(`duplicate position/move pair at ${row.id}`)
    }
    positionMoves.add(key)
  }

  const phases = new Set(rows.map((row) => row.phase))
  for (const phase of PHASES) {
    if (!phases.has(phase as CorpusPhase)) problems.push(`corpus has no ${phase} positions`)
  }
  const tags = new Set(rows.flatMap((row) => row.tags))
  for (const tag of REQUIRED_TAGS) {
    if (!tags.has(tag)) problems.push(`corpus has no ${tag} position`)
  }

  return problems
}

export const loadCorpus = (): CorpusFile => {
  const problems = corpusProblems(corpusJson)
  if (problems.length > 0) {
    throw new Error(`invalid grading corpus:\n- ${problems.join('\n- ')}`)
  }
  return corpusJson as CorpusFile
}

export const corpusBytes = (): Buffer =>
  readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'corpus.json'))

export const corpusSha256 = (): string =>
  createHash('sha256').update(corpusBytes()).digest('hex')
