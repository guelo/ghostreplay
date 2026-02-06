import { readFile, writeFile } from 'node:fs/promises'
import { performance } from 'node:perf_hooks'
import { Chess } from 'chess.js'

const INPUT_PATH = 'public/data/openings/eco.json'
const OUTPUT_PATH = 'public/data/openings/eco.byPosition.json'

const normalizeFen = (fen) => {
  const board = new Chess(fen)
  return board.fen().split(' ').slice(0, 4).join(' ')
}

const compareCandidates = (a, b) => {
  if (a.ply !== b.ply) {
    return b.ply - a.ply
  }
  if (a.linePlyCount !== b.linePlyCount) {
    // Prefer the most general line at the current position.
    return a.linePlyCount - b.linePlyCount
  }
  const ecoCompare = a.entry.eco.localeCompare(b.entry.eco)
  if (ecoCompare !== 0) {
    return ecoCompare
  }
  return a.entry.name.localeCompare(b.entry.name)
}

const buildPositionIndex = (entries) => {
  const bestByPosition = new Map()
  for (const entry of entries) {
    const moves = entry.uci.trim().split(/\s+/).filter(Boolean)
    if (moves.length === 0) {
      continue
    }

    const board = new Chess()
    for (let ply = 0; ply < moves.length; ply += 1) {
      const move = moves[ply]
      const parsed = board.move({
        from: move.slice(0, 2),
        to: move.slice(2, 4),
        promotion: move.slice(4, 5) || undefined,
      })
      if (!parsed) {
        break
      }

      const position = normalizeFen(board.fen())
      const candidate = {
        entry,
        ply: ply + 1,
        linePlyCount: moves.length,
      }
      const current = bestByPosition.get(position)
      if (!current || compareCandidates(candidate, current) < 0) {
        bestByPosition.set(position, candidate)
      }
    }
  }

  return bestByPosition
}

const main = async () => {
  const inputRaw = await readFile(INPUT_PATH, 'utf8')
  const payload = JSON.parse(inputRaw)
  const entries = payload.entries

  if (!Array.isArray(entries)) {
    throw new Error('Opening book payload is missing entries array.')
  }

  const startedAt = performance.now()
  const bestByPosition = buildPositionIndex(entries)
  const elapsedMs = performance.now() - startedAt

  const orderedByPosition = Object.fromEntries(
    [...bestByPosition.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([position, candidate]) => [
        position,
        {
          eco: candidate.entry.eco,
          name: candidate.entry.name,
        },
      ]),
  )

  const output = {
    dataset: payload.dataset,
    source_commit: payload.source_commit,
    position_count: Object.keys(orderedByPosition).length,
    by_position: orderedByPosition,
  }

  await writeFile(OUTPUT_PATH, `${JSON.stringify(output)}\n`, 'utf8')
  console.log(
    `Built ${output.position_count} opening positions in ${elapsedMs.toFixed(0)}ms -> ${OUTPUT_PATH}`,
  )
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
