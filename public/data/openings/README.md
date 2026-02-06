# ECO Opening Book Dataset

This directory vendors the full ECO opening dataset used by the client.

- File: `eco.json`
- Entries: `3641`
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

This keeps the dataset static and local so the frontend has no runtime network dependency for opening data.
