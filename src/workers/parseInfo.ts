import type { EngineInfo, EngineScoreBound } from './stockfishMessages'

/** Read the numeric token immediately after `token`, or undefined if absent/NaN. */
const readNumberAfter = (tokens: string[], token: string): number | undefined => {
  const index = tokens.indexOf(token)
  if (index === -1) {
    return undefined
  }
  const value = Number(tokens[index + 1])
  return Number.isNaN(value) ? undefined : value
}

/**
 * An aspiration-window re-search reports its score as a bound rather than a
 * settled value. UCI places `lowerbound`/`upperbound` after the score value, but
 * a scan of the whole line is safe: no PV move can collide with either keyword.
 */
const readScoreBound = (tokens: string[]): EngineScoreBound => {
  if (tokens.includes('lowerbound')) {
    return 'lower'
  }
  if (tokens.includes('upperbound')) {
    return 'upper'
  }
  return 'exact'
}

/** Parse a UCI "info" line into an EngineInfo object, or null if not useful. */
export function parseUciInfoLine(line: string): EngineInfo | null {
  if (!line.startsWith('info')) {
    return null
  }

  const tokens = line.split(' ')
  const info: EngineInfo = {}

  const depth = readNumberAfter(tokens, 'depth')
  if (depth !== undefined) {
    info.depth = depth
  }

  const seldepth = readNumberAfter(tokens, 'seldepth')
  if (seldepth !== undefined) {
    info.seldepth = seldepth
  }

  const scoreIndex = tokens.indexOf('score')

  if (scoreIndex !== -1) {
    const scoreType = tokens[scoreIndex + 1]
    const scoreValue = Number(tokens[scoreIndex + 2])

    if (!Number.isNaN(scoreValue) && (scoreType === 'cp' || scoreType === 'mate')) {
      info.score = {
        type: scoreType,
        value: scoreValue,
      }
      info.bound = readScoreBound(tokens)
    }
  }

  const multipv = readNumberAfter(tokens, 'multipv')
  if (multipv !== undefined) {
    info.multipv = multipv
  }

  const nodes = readNumberAfter(tokens, 'nodes')
  if (nodes !== undefined) {
    info.nodes = nodes
  }

  const nps = readNumberAfter(tokens, 'nps')
  if (nps !== undefined) {
    info.nps = nps
  }

  const time = readNumberAfter(tokens, 'time')
  if (time !== undefined) {
    info.time = time
  }

  const hashfull = readNumberAfter(tokens, 'hashfull')
  if (hashfull !== undefined) {
    info.hashfull = hashfull
  }

  const pvIndex = tokens.indexOf('pv')

  if (pvIndex !== -1) {
    const pv = tokens.slice(pvIndex + 1)
    if (pv.length > 0) {
      info.pv = pv
    }
  }

  if (info.depth || info.score || info.pv) {
    return info
  }

  return null
}
