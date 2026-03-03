# Precompute Opening Book Analysis

`precompute_openings.py` runs Stockfish against every position in the opening
book (`eco.json`, ~7,833 unique positions) and writes results to the
`analysis_cache` table.

## Running locally

```bash
cd backend
python scripts/precompute_openings.py --depth 24 --workers 2 --verbose
```

At depth 24 with 2 workers on a laptop this takes **~45 hours**. Scaling is
roughly linear with `--workers` (each worker runs its own Stockfish process on a
single thread).

## Running on a server

For faster results, run the script on a machine with more cores.

### 1. Set up the environment

```bash
git clone <repo> && cd ghostreplay/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
apt install stockfish   # or brew install stockfish
```

### 2. Run with a standalone SQLite file

Write to an isolated database so it's easy to transfer:

```bash
python scripts/precompute_openings.py \
  --database-url sqlite:///analysis_cache.db \
  --depth 24 \
  --workers 12 \
  --verbose
```

A 16-core machine with `--workers 12` cuts the time to roughly **7–8 hours**.

### 3. Copy results to your dev machine

```bash
scp server:ghostreplay/backend/analysis_cache.db .
```

Import into your local dev database:

```bash
sqlite3 analysis_cache.db ".dump analysis_cache" | sqlite3 ghostreplay.db
```

The upsert logic means running this multiple times is safe — existing rows are
updated, not duplicated.

### Alternative: write directly to PostgreSQL

If the server can reach your PostgreSQL instance:

```bash
python scripts/precompute_openings.py \
  --database-url postgresql+psycopg://user:pass@host:5432/ghostreplay \
  --depth 24 \
  --workers 12
```

## Resumability

The script does **not** currently skip already-cached positions. If interrupted,
it re-analyzes everything from scratch. The upsert ensures no duplicates, but
the work is repeated.

To resume manually after a partial run, you can dump the cached positions and
filter them out, or add skip logic to the script (query existing
`(fen_before, move_uci)` pairs before starting).

## CLI reference

| Flag | Default | Description |
|------|---------|-------------|
| `--database-url` | `postgresql+psycopg://postgres:postgres@localhost:5432/ghostreplay` | SQLAlchemy DB URL |
| `--eco-path` | `public/data/openings/eco.json` | Path to opening book |
| `--depth` | 24 | Stockfish search depth |
| `--workers` | 1 | Parallel Stockfish processes |
| `--stockfish` | `stockfish` | Path to Stockfish binary |
| `--verbose` / `-v` | off | Log every position (vs every 50) |
| `--dry-run` | off | Extract positions without analysis |
