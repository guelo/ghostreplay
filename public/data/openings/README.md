# ECO Opening Book Dataset

This directory vendors the full ECO opening dataset used by the client.

- Files:
  - `eco.json`
  - `eco.byPosition.json`
- Entries: `15374`
- Indexed positions: `15510`
- Source repository: `https://github.com/JeffML/eco.json`
- Source commit: `f398993004c7a84701e24691573af3c9bd196ffd`
- Upstream license: Public domain ECO data

## Provenance

`eco.json` was generated from upstream files `ecoA.json` through `ecoE.json` plus `eco_interpolated.json` using:

```bash
npm run openings:ingest
```

The ingest script (`scripts/ingest-eco-data.mjs`) downloads, transforms, deduplicates, and writes entries with fields:

- `eco`
- `name`
- `pgn`
- `uci`
- `epd`

It also rebuilds `eco.byPosition.json` automatically.

`eco.byPosition.json` can also be regenerated independently from `eco.json` using:

```bash
npm run openings:build-index
```

This keeps opening data static and local while avoiding runtime index construction on the main thread.
