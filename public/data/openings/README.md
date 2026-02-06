# ECO Opening Book Dataset

This directory vendors the full ECO opening dataset used by the client.

- Files:
  - `eco.json`
  - `eco.byPosition.json`
- Entries: `3641`
- Indexed positions: `7484`
- Source repository: `https://github.com/lichess-org/chess-openings`
- Source commit: `89797fcc13ad1779411d21bdf8436372264f02ad` (HEAD on 2026-02-06)
- Upstream license: CC0 Public Domain Dedication (`https://creativecommons.org/publicdomain/zero/1.0/`)

## Provenance

`eco.json` was generated from upstream files `a.tsv` through `e.tsv` after normalizing with the upstream build script `bin/gen.py` to include:

- `eco`
- `name`
- `pgn`
- `uci`
- `epd`

`eco.byPosition.json` is generated from `eco.json` using:

```bash
npm run openings:build-index
```

This keeps opening data static and local while avoiding runtime index construction on the main thread.
