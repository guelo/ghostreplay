import { readFile, writeFile } from 'node:fs/promises'
import { execSync } from 'node:child_process'
import { performance } from 'node:perf_hooks'
import { Chess } from 'chess.js'

const JEFF_ECO_COMMIT = 'f398993004c7a84701e24691573af3c9bd196ffd'
const BASE_URL = `https://raw.githubusercontent.com/JeffML/eco.json/${JEFF_ECO_COMMIT}`
const PRIMARY_FILES = ['ecoA.json', 'ecoB.json', 'ecoC.json', 'ecoD.json', 'ecoE.json']
const INTERPOLATED_FILE = 'eco_interpolated.json'
const ALL_FILES = [...PRIMARY_FILES, INTERPOLATED_FILE]

const OUTPUT_PATH = 'public/data/openings/eco.json'
const INDEX_PATH = 'public/data/openings/eco.byPosition.json'

// ---------------------------------------------------------------------------
// Phase 1 — Download
// ---------------------------------------------------------------------------

const download = async (filename) => {
  const url = `${BASE_URL}/${filename}`
  console.log(`  Fetching ${filename}...`)
  const res = await fetch(url)
  if (!res.ok) {
    throw new Error(`Failed to fetch ${url}: ${res.status} ${res.statusText}`)
  }
  return { filename, data: await res.json() }
}

// ---------------------------------------------------------------------------
// Phase 2 — Merge & deduplicate by raw FEN key
// ---------------------------------------------------------------------------

const mergeFiles = (results) => {
  const merged = new Map()
  const stats = {}

  for (const { filename, data } of results) {
    const isInterpolated = filename === INTERPOLATED_FILE
    const entries = Object.entries(data)
    let added = 0

    for (const [fen, entry] of entries) {
      if (isInterpolated && merged.has(fen)) {
        continue // primary entries take precedence
      }
      merged.set(fen, { ...entry, _src: entry.src || (isInterpolated ? 'interpolated' : 'unknown') })
      added += 1
    }

    stats[filename] = { total: entries.length, added }
    console.log(`  ${filename}: ${entries.length} entries, ${added} added`)
  }

  return { merged, stats }
}

// ---------------------------------------------------------------------------
// Phase 3 — Transform (PGN → UCI, canonical EPD via chess.js replay)
// ---------------------------------------------------------------------------

const canonicalEpd = (fen) => {
  const board = new Chess(fen)
  return board.fen().split(' ').slice(0, 4).join(' ')
}

const transformEntry = (rawFen, entry) => {
  if (!entry.eco || !entry.name || !entry.moves) {
    return null // skip incomplete entries
  }

  const pgn = entry.moves.trim()
  if (!pgn) {
    return null
  }

  const board = new Chess()
  try {
    board.loadPgn(pgn)
  } catch (e) {
    throw new Error(
      `loadPgn() failed for ${entry.eco} "${entry.name}": ${pgn}\n  ${e.message}`
    )
  }

  // Derive UCI from replayed moves
  const history = board.history({ verbose: true })
  const uci = history.map((m) => m.from + m.to + (m.promotion || '')).join(' ')

  // Derive canonical EPD from replayed board state
  const replayedEpd = board.fen().split(' ').slice(0, 4).join(' ')

  // Hard fail: canonical EPD from replay must match canonical EPD from raw FEN key
  const rawEpd = canonicalEpd(rawFen)
  if (replayedEpd !== rawEpd) {
    throw new Error(
      `FEN mismatch for ${entry.eco} "${entry.name}":\n` +
      `  raw FEN:      ${rawFen}\n` +
      `  raw EPD:      ${rawEpd}\n` +
      `  replayed EPD: ${replayedEpd}\n` +
      `  PGN:          ${pgn}`
    )
  }

  return {
    eco: entry.eco,
    name: entry.name,
    pgn,
    uci,
    epd: replayedEpd,
    _src: entry._src,
    _moveCount: history.length,
  }
}

// ---------------------------------------------------------------------------
// Phase 4 — Deduplicate by canonical EPD
// ---------------------------------------------------------------------------

const deduplicateByEpd = (entries) => {
  const byEpd = new Map()

  for (const entry of entries) {
    const existing = byEpd.get(entry.epd)
    if (!existing) {
      byEpd.set(entry.epd, entry)
      continue
    }

    // Tie-breaking: prefer primary > interpolated, then shorter move count, then ECO, then name
    const winner = pickWinner(existing, entry)
    byEpd.set(entry.epd, winner)
  }

  const collisions = entries.length - byEpd.size
  if (collisions > 0) {
    console.log(`  EPD collisions resolved: ${collisions}`)
  }

  return [...byEpd.values()]
}

const pickWinner = (a, b) => {
  // Prefer primary (eco_tsv) over interpolated
  const aIsPrimary = a._src === 'eco_tsv' ? 0 : 1
  const bIsPrimary = b._src === 'eco_tsv' ? 0 : 1
  if (aIsPrimary !== bIsPrimary) return aIsPrimary < bIsPrimary ? a : b

  // Prefer shorter move count (more general opening)
  if (a._moveCount !== b._moveCount) return a._moveCount < b._moveCount ? a : b

  // ECO code
  const ecoComp = a.eco.localeCompare(b.eco)
  if (ecoComp !== 0) return ecoComp < 0 ? a : b

  // Name
  return a.name.localeCompare(b.name) <= 0 ? a : b
}

// ---------------------------------------------------------------------------
// Phase 5 — Sort & Write
// ---------------------------------------------------------------------------

const sortEntries = (entries) =>
  entries.sort((a, b) => {
    const ecoComp = a.eco.localeCompare(b.eco)
    if (ecoComp !== 0) return ecoComp
    if (a._moveCount !== b._moveCount) return a._moveCount - b._moveCount
    return a.name.localeCompare(b.name)
  })

const stripInternalFields = (entry) => ({
  eco: entry.eco,
  name: entry.name,
  pgn: entry.pgn,
  uci: entry.uci,
  epd: entry.epd,
})

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const main = async () => {
  const startedAt = performance.now()

  // Phase 1: Download
  console.log('Phase 1: Downloading...')
  const results = await Promise.all(ALL_FILES.map(download))

  // Phase 2: Merge
  console.log('Phase 2: Merging...')
  const { merged } = mergeFiles(results)
  console.log(`  Merged total: ${merged.size} unique FEN keys`)

  // Phase 3: Transform
  console.log('Phase 3: Transforming...')
  const transformed = []
  let skipped = 0
  for (const [rawFen, entry] of merged) {
    const result = transformEntry(rawFen, entry)
    if (result) {
      transformed.push(result)
    } else {
      skipped += 1
    }
  }
  console.log(`  Transformed: ${transformed.length}, skipped (incomplete): ${skipped}`)

  // Phase 4: Deduplicate by canonical EPD
  console.log('Phase 4: Deduplicating by canonical EPD...')
  const deduped = deduplicateByEpd(transformed)

  // Assert uniqueness
  const epdSet = new Set(deduped.map((e) => e.epd))
  if (epdSet.size !== deduped.length) {
    throw new Error(`EPD uniqueness assertion failed: ${deduped.length} entries but ${epdSet.size} unique EPDs`)
  }
  console.log(`  Final entries: ${deduped.length}`)

  // Phase 5: Sort & Write
  console.log('Phase 5: Sorting and writing...')
  sortEntries(deduped)

  const payload = {
    dataset: 'JeffML/eco.json',
    source_commit: JEFF_ECO_COMMIT,
    entry_count: deduped.length,
    entries: deduped.map(stripInternalFields),
  }

  await writeFile(OUTPUT_PATH, JSON.stringify(payload) + '\n', 'utf8')
  console.log(`  Wrote ${OUTPUT_PATH} (${deduped.length} entries)`)

  // Rebuild position index
  console.log('Phase 6: Rebuilding position index...')
  execSync('node scripts/build-opening-position-index.mjs', { stdio: 'inherit' })

  // Validate metadata consistency
  const indexRaw = await readFile(INDEX_PATH, 'utf8')
  const index = JSON.parse(indexRaw)
  if (index.dataset !== payload.dataset || index.source_commit !== payload.source_commit) {
    throw new Error('Metadata mismatch between eco.json and eco.byPosition.json')
  }

  const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1)
  console.log(`\nDone in ${elapsed}s: ${deduped.length} entries, ${index.position_count} indexed positions`)
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
