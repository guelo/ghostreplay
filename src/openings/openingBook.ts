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

type OpeningPositionIndexEntry = {
  eco: string
  name: string
}

type OpeningPositionIndexFile = {
  dataset: string
  source_commit: string
  position_count: number
  by_position: Record<string, OpeningPositionIndexEntry>
}

export type OpeningBook = {
  dataset: string
  sourceCommit: string
  entries: OpeningBookEntry[]
  byEpd: Map<string, OpeningBookEntry>
  byPosition: Map<string, OpeningLookupResult>
}

const OPENING_BOOK_URL = '/data/openings/eco.json'
const OPENING_POSITION_INDEX_URL = '/data/openings/eco.byPosition.json'
const OPENING_SOURCE = 'eco'

let openingBookPromise: Promise<OpeningBook> | null = null
const openingLookupCache = new Map<string, OpeningLookupResult | null>()

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

const toLookupResult = (
  entry: Pick<OpeningBookEntry, 'eco' | 'name'>,
): OpeningLookupResult => ({
  eco: entry.eco,
  name: entry.name,
  variation: parseVariation(entry.name),
  source: OPENING_SOURCE,
})

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

const assertOpeningPositionIndex = (value: unknown): OpeningPositionIndexFile => {
  if (!value || typeof value !== 'object') {
    throw new Error('Opening position index payload is invalid.')
  }

  const parsed = value as Partial<OpeningPositionIndexFile>
  if (
    typeof parsed.dataset !== 'string' ||
    typeof parsed.source_commit !== 'string' ||
    typeof parsed.position_count !== 'number' ||
    !parsed.by_position ||
    typeof parsed.by_position !== 'object'
  ) {
    throw new Error('Opening position index payload is malformed.')
  }

  return parsed as OpeningPositionIndexFile
}

const fetchJson = async (url: string, label: string): Promise<unknown> => {
  const response = await fetch(url)
  if (!response.ok) {
    throw new Error(`Failed to load ${label} (${response.status} ${response.statusText})`)
  }
  return response.json()
}

export const getOpeningBook = async (): Promise<OpeningBook> => {
  if (!openingBookPromise) {
    openingBookPromise = Promise.all([
      fetchJson(OPENING_BOOK_URL, 'opening book'),
      fetchJson(OPENING_POSITION_INDEX_URL, 'opening position index'),
    ])
      .then(([bookPayload, indexPayload]) => {
        const file = assertOpeningBook(bookPayload)
        if (file.entries.length !== file.entry_count) {
          throw new Error(
            `Opening book count mismatch (expected ${file.entry_count}, got ${file.entries.length})`,
          )
        }

        const index = assertOpeningPositionIndex(indexPayload)
        if (
          index.dataset !== file.dataset ||
          index.source_commit !== file.source_commit
        ) {
          throw new Error('Opening position index metadata mismatch.')
        }

        const positionEntries = Object.entries(index.by_position)
        if (positionEntries.length !== index.position_count) {
          throw new Error(
            `Opening position index count mismatch (expected ${index.position_count}, got ${positionEntries.length})`,
          )
        }

        return {
          dataset: file.dataset,
          sourceCommit: file.source_commit,
          entries: file.entries,
          byEpd: new Map(file.entries.map((entry) => [entry.epd, entry])),
          byPosition: new Map(
            positionEntries.map(([position, entry]) => [
              position,
              toLookupResult(entry),
            ]),
          ),
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
