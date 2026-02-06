# Opening Book Loader

The client ships a vendored opening book and precomputed position index:

- `public/data/openings/eco.json`
- `public/data/openings/eco.byPosition.json`

Use `getOpeningBook()` to load and cache it:

```ts
import { getOpeningBook } from '../src/openings/openingBook'

const book = await getOpeningBook()
const opening = book.byEpd.get(epd)
```

Use `lookupOpeningByFen()` for transposition-aware matching from a live FEN:

```ts
import { lookupOpeningByFen } from '../src/openings/openingBook'

const opening = await lookupOpeningByFen(fen)
// { eco, name, variation?, source } | null
```

## API

- `getOpeningBook(): Promise<OpeningBook>`
- `OpeningBook.entries`: all opening entries from the dataset
- `OpeningBook.byEpd`: `Map<string, OpeningBookEntry>` indexed by EPD
- `OpeningBook.byPosition`: `Map<string, OpeningLookupResult>` indexed by normalized FEN/EPD
- `lookupOpeningByFen(fen: string): Promise<OpeningLookupResult | null>`

`lookupOpeningByFen()` normalizes incoming FEN (fields 1-4 with canonical en-passant), performs O(1) transposition-aware lookups against the precomputed index, and memoizes per-position results.

`getOpeningBook()` fetches both files once, memoizes the parsed result, and resets the cache if loading fails so callers can retry.
