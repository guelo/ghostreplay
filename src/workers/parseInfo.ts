import type { EngineInfo } from './stockfishMessages'

/** Parse a UCI "info" line into an EngineInfo object, or null if not useful. */
export function parseUciInfoLine(line: string): EngineInfo | null {
  if (!line.startsWith('info')) {
    return null
  }

  const tokens = line.split(' ')
  const info: EngineInfo = {}
  const depthIndex = tokens.indexOf('depth')

  if (depthIndex !== -1) {
    const depthValue = Number(tokens[depthIndex + 1])
    if (!Number.isNaN(depthValue)) {
      info.depth = depthValue
    }
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
    }
  }

  const multipvIndex = tokens.indexOf('multipv')

  if (multipvIndex !== -1) {
    const multipvValue = Number(tokens[multipvIndex + 1])
    if (!Number.isNaN(multipvValue)) {
      info.multipv = multipvValue
    }
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
