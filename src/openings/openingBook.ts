import { Chess } from 'chess.js'

export type OpeningBookEntry = {
  eco: string
  name: string
  pgn: string
  uci: string
  epd: string
}

type OpeningBookFile = {
  dataset: string
  source_commit: string
  entry_count: number
  entries: OpeningBookEntry[]
}

export type OpeningBook = {
  dataset: string
  sourceCommit: string
  entries: OpeningBookEntry[]
  byEpd: Map<string, OpeningBookEntry>
  byPosition: Map<string, OpeningLookupResult>
}

const OPENING_BOOK_URL = '/data/openings/eco.json'
const OPENING_SOURCE = 'eco'

let openingBookPromise: Promise<OpeningBook> | null = null
const openingLookupCache = new Map<string, OpeningLookupResult | null>()

type OpeningCandidate = {
  entry: OpeningBookEntry
  ply: number
  linePlyCount: number
}

export type OpeningLookupResult = {
  eco: string
  name: string
  variation?: string
  source: string
}

const normalizeFen = (fen: string): string => {
  const board = new Chess(fen)
  return board.fen().split(' ').slice(0, 4).join(' ')
}

const parseVariation = (name: string): string | undefined => {
  const separator = name.indexOf(':')
  if (separator === -1) {
    return undefined
  }
  return name.slice(separator + 1).trim() || undefined
}

const toLookupResult = (entry: OpeningBookEntry): OpeningLookupResult => ({
  eco: entry.eco,
  name: entry.name,
  variation: parseVariation(entry.name),
  source: OPENING_SOURCE,
})

const compareCandidates = (a: OpeningCandidate, b: OpeningCandidate): number => {
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

const buildPositionIndex = (
  entries: OpeningBookEntry[],
): Map<string, OpeningLookupResult> => {
  const bestByPosition = new Map<string, OpeningCandidate>()
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
      const candidate: OpeningCandidate = {
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

  return new Map(
    [...bestByPosition.entries()].map(([position, candidate]) => [
      position,
      toLookupResult(candidate.entry),
    ]),
  )
}

const assertOpeningBook = (value: unknown): OpeningBookFile => {
  if (!value || typeof value !== 'object') {
    throw new Error('Opening book payload is invalid.')
  }

  const parsed = value as Partial<OpeningBookFile>
  if (
    typeof parsed.dataset !== 'string' ||
    typeof parsed.source_commit !== 'string' ||
    typeof parsed.entry_count !== 'number' ||
    !Array.isArray(parsed.entries)
  ) {
    throw new Error('Opening book payload is malformed.')
  }

  return parsed as OpeningBookFile
}

export const getOpeningBook = async (): Promise<OpeningBook> => {
  if (!openingBookPromise) {
    openingBookPromise = fetch(OPENING_BOOK_URL)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(
            `Failed to load opening book (${response.status} ${response.statusText})`,
          )
        }
        return response.json()
      })
      .then((payload) => {
        const file = assertOpeningBook(payload)
        if (file.entries.length !== file.entry_count) {
          throw new Error(
            `Opening book count mismatch (expected ${file.entry_count}, got ${file.entries.length})`,
          )
        }

        return {
          dataset: file.dataset,
          sourceCommit: file.source_commit,
          entries: file.entries,
          byEpd: new Map(file.entries.map((entry) => [entry.epd, entry])),
          byPosition: buildPositionIndex(file.entries),
        }
      })
      .catch((error) => {
        openingBookPromise = null
        throw error
      })
  }

  return openingBookPromise
}

export const lookupOpeningByFen = async (
  fen: string,
): Promise<OpeningLookupResult | null> => {
  const normalizedFen = normalizeFen(fen)
  if (openingLookupCache.has(normalizedFen)) {
    return openingLookupCache.get(normalizedFen) ?? null
  }

  const book = await getOpeningBook()
  const opening = book.byPosition.get(normalizedFen) ?? null
  openingLookupCache.set(normalizedFen, opening)
  return opening
}

export const resetOpeningBookCacheForTests = (): void => {
  openingBookPromise = null
  openingLookupCache.clear()
}
