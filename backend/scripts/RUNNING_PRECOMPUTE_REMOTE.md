# Running the canonical opening precompute on a remote Linux box

`scripts/precompute_openings.py` analyzes every opening-book position with
Stockfish at the pinned canonical depth and upserts resolver-complete rows into
`analysis_cache`. A full run is CPU-bound and takes hours, so it's run on a beefy
Linux box rather than a laptop, writing over the network to the production DB.

This doc captures the end-to-end procedure, including the non-obvious gotchas.
It is written to be repeated when a new Stockfish release (e.g. SF19) requires a
new canonical profile.

---

## Key concepts (read first)

- **The run is pinned to an exact engine binary.** The script SHA-256-hashes the
  Stockfish executable it launches and **aborts** unless that hash resolves to an
  `active` + `authoritative` profile in `app/analysis_profiles.py`
  (`resolve_profile`). Profiles are pinned by committed JSON manifests under
  `app/canonical_profiles/`.
- **The binary is OS/arch specific.** A macOS (Mach-O) Stockfish and a Linux
  (ELF) Stockfish of the *same* release have *different* executable hashes, so
  each platform needs its own profile entry. That's why there's a
  `canonical-sf18-depth24-linux-v1` profile distinct from the macOS one.
- **Prod must recognize the profile.** The serving/quality path verifies rows
  against the same in-process registry (`get_profile` + `identity_verified` in
  `app/analysis_cache_policy.py`). If the profile the rows are stamped with is
  not in the **deployed** prod code, prod treats its own canonical rows as
  unverified and ignores them. So the profile change must be committed and
  deployed to prod, not just present on the Linux box.
- **The script does NOT read `DATABASE_URL` from the environment.** It uses the
  `--database-url` flag, which defaults to a hardcoded localhost URL. You must
  pass the prod URL explicitly, and it must name the **psycopg v3** driver:
  `postgresql+psycopg://...` (a bare `postgresql://...` selects psycopg2, which
  is not installed).
- **Use the Railway PUBLIC URL.** From an external box the private
  `*.railway.internal` host is unreachable; use `DATABASE_PUBLIC_URL`
  (`*.proxy.rlwy.net:<port>`) and append `?sslmode=require`.
- **Concurrent prod use is safe.** Writes are row-locked and quality-arbitrated;
  reads are never blocked (Postgres MVCC). The cache upgrades position-by-position
  while the app stays up.

---

## Procedure

### 0. Check out the repo on the Linux box

```bash
git clone <ghostreplay-remote> ghostreplay   # or: cd ghostreplay && git pull
cd ghostreplay/backend
git log --oneline -1     # confirm it includes the precompute + profile work
```

### 1. Get the Linux Stockfish binary and capture its hash

Prefer the official release binary (more reproducible than a local build). Match
your CPU (`bmi2`/`avx2` for modern x86-64).

```bash
cd ~
SFVER=sf_18                     # bump for new releases, e.g. sf_19
wget https://github.com/official-stockfish/Stockfish/releases/download/$SFVER/stockfish-ubuntu-x86-64-bmi2.tar
tar xf stockfish-ubuntu-x86-64-bmi2.tar
export SF=~/stockfish/stockfish-ubuntu-x86-64-bmi2

"$SF" <<< $'uci\nquit' | grep -iE "Stockfish|EvalFile"   # confirm version + net filenames
sha256sum "$SF"                                            # -> engine_build for the manifest
```

Note the **executable sha256** and the two embedded NNUE filenames
(`EvalFile` / `EvalFileSmall`). For the same release these net filenames match
the existing manifest, so only `engine_build` (and `architecture`) change.

### 2. Register a canonical profile for this binary

Create `app/canonical_profiles/canonical-<engine>-depth<N>-linux-v1.json` by
copying the most recent one and updating `profile_id`, `engine_build` (step 1
hash), `architecture`, and — for a new Stockfish release — `engine_version`,
`release_tag`, `source_commit`, `artifact_url`, and the `eval_file*` /
`eval_file_id*` network identities (full SHA-256 of each official NNUE net,
fetched from data.stockfishchess.org/nn).

Keep `authoritative: true`, `active: true`, and the same `dominates` list.

Register it in `app/analysis_profiles.py`:

```python
_CANONICAL_LINUX = _load_manifest("canonical-sf18-depth24-linux-v1")
...
_REGISTRY: dict[str, Profile] = {
    p.profile_id: p for p in (_CANONICAL, _CANONICAL_LINUX, _BROWSER, _JEFFML)
}
```

> Note on immutability: a profile manifest must not be mutated once production
> rows reference it. For a brand-new platform/release with no rows yet, adding a
> new profile (or editing an unreferenced one) is fine. For a *new Stockfish
> version*, always add a NEW profile (`-v2` / new id) — never repoint an existing
> one that rows already use.

### 3. Validate the engine resolves (before the hours-long run)

`--dry-run` does NOT exercise the engine or DB — it only parses the book. To
actually check identity resolution, run the resolver directly:

```bash
cd ~/ghostreplay/backend
source .venv/bin/activate     # set up in step 4 if not yet
python - <<'PY'
from scripts.precompute_openings import observe_engine_identity
from app.analysis_profiles import resolve_profile
import os
obs = observe_engine_identity(os.environ["SF"], 24)
for k, v in obs.items():
    print(f"  {k}: {v}")
print("resolved profile:", resolve_profile(obs))
PY
```

Expect `engine_name: Stockfish`, the right `engine_version`, your `engine_build`
hash, the expected net filenames, and a non-`None` `resolved profile`. If it
prints `None`, the run will abort — diff the observed identity against the
manifest field-by-field.

### 4. Commit + deploy the profile to prod

```bash
git checkout -b linux-canonical-profile
git add app/canonical_profiles/canonical-*-linux-v1.json app/analysis_profiles.py
git commit -m "feat(backend): register Linux SF canonical profile"
git push -u origin linux-canonical-profile
# merge to master and let Railway deploy. Prod must run this registry before the
# generated rows will be trusted at read time.
```

### 5. Python env + deps

```bash
cd ~/ghostreplay/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 6. Point at prod DB and migrate

Get `DATABASE_PUBLIC_URL` from Railway (Postgres service -> Connect). Note the
psycopg-v3 driver in the scheme:

```bash
# raw Railway URL is postgresql://... ; rewrite to the psycopg v3 driver:
export DATABASE_URL="postgresql+psycopg://postgres:<pw>@<proxy>.proxy.rlwy.net:<port>/railway?sslmode=require"

alembic upgrade head     # additive analysis_cache columns; safe with app running
                         # (alembic reads DATABASE_URL from the env and normalizes the scheme itself)
```

### 7. Run it (detached, hours)

Use tmux so an SSH drop doesn't kill the run. The script needs the URL passed
explicitly via `--database-url` (it does NOT read the env var):

```bash
tmux new -s precompute
source .venv/bin/activate
export SF=~/stockfish/stockfish-ubuntu-x86-64-bmi2
DB="postgresql+psycopg://postgres:<pw>@<proxy>.proxy.rlwy.net:<port>/railway?sslmode=require"

python scripts/precompute_openings.py \
  --stockfish "$SF" \
  --database-url "$DB" \
  --workers <physical-core-count> \
  --manifest-out ~/precompute-manifest.json -v 2>&1 | tee ~/precompute.log

# detach: Ctrl-b then d   |   reattach: tmux attach -t precompute
```

`--workers` ≈ physical cores (each worker is one single-threaded SF at 128MB
hash). Default is 1 — bump it up.

**Resume is automatic.** On startup the script skips any position that already
has an authoritative resolver-complete-v2 row for the resolved profile (matched
by profile id + contract + manifest digest). So if a run aborts partway, just
**re-run the exact same command** — it processes only the remaining positions and
each run makes forward progress. The log prints `Resume: N/M positions already
stored ...`. For a brand-new profile (e.g. SF19) nothing is stored yet, so resume
is a no-op and the full book is analyzed. Pass `--no-resume` to force a full
re-analysis. Resume only skips rows that pass the same trust gate prod uses at
read time (full identity match + v2 contract validation); a malformed row is
re-analyzed rather than skipped.

No DB writes happen *during* the hours of analysis — all results are held in
memory and flushed only after every worker joins. That flush then commits in
independent ~100-row transactions, so a crash mid-flush (or an aborted run) can
leave some batches committed and others not. That's fine: resume validates each
stored row independently on the next run, so partial commits simply become fewer
remaining positions.

> One slow search aborts the whole run. `SEARCH_DEADLINE_S` (default **600s**,
> override with `--search-deadline`) is **per search**, and each position runs up
> to 3 searches (root, played-move, best-move) back-to-back on one persistent
> engine. A *single* search exceeding the deadline raises `EngineTimeout`, which
> calls `abort.set()` and stops all workers (in-flight/completed results are still
> written on join, so resume recovers them).
>
> The slowest opening-book positions are simply expensive at depth 24. Example:
> `a2a3` at `N1bk3r/pp1p1ppp/2n2n2/8/1b6/5B1q/PPPN1P1P/R1BQK2R w KQ - 1 11` takes
> ~225s on a fully idle box (root ~11s, played ~152s, best ~62s), so at the old
> 300s-per-search wall any load tipped its 152s search over and it aborted every
> run regardless of `--workers`. The default is now 600s, which leaves headroom
> for these honest-but-slow searches; with that, a timeout means a genuinely
> dead/hung engine. (The frontend `analysisWorker.ts` never hits this: it searches
> at `go depth 17`, far cheaper than the canonical depth 24. The precompute is the
> only thing that walks the whole book this deep. The searches are intentionally
> NOT reset between each other — `ucinewgame` is sent once per position and never
> between the 3 searches — to stay bit-identical to the frontend analyzer; do not
> "fix" the slowness by adding a reset, as TT carryover can change the depth-24
> result and break the contract.)
>
> To check whether a recurring timeout is a slow position or a real hang, time it
> idle with the analyzer itself — do NOT use a `stockfish <<EOF ... go depth 24`
> heredoc: stdin EOF makes Stockfish abort the search early and report a bogus
> sub-second time. Use the real code path, which keeps the engine alive until
> `bestmove`:
>
> ```bash
> python - <<'PY'
> import os, time
> from scripts.precompute_openings import analyze_position, PositionToAnalyze
> pos = PositionToAnalyze(fen_before="<fen>", move_uci="<uci>", move_san="x")
> t = time.time()
> res, outcome = analyze_position(pos, 24, os.environ["SF"])
> print(f"{outcome} {time.time()-t:.1f}s")
> PY
> ```
>
> If that idle time is already a large fraction of the deadline, raise
> `--search-deadline` (and/or lower `--workers` for less load-induced slowdown)
> rather than assuming a hang.

### 8. Verify and record

```bash
grep -E '"status"|verify_failures|errored|unprocessed|write_failures|processed' ~/precompute-manifest.json
```

Want `status: ok` and zeros across the failure counts (the script fails closed,
so a clean exit means rows were written and read-back-verified). Then spot-check
in the app that a previously-flaky opening returns a stable best move.

Commit the run manifest for provenance and note the run (profile id + counts) on
the relevant beads issue.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Engine identity does not match any active canonical profile` | binary hash / net filenames don't match the manifest, or profile not `active`+`authoritative` | re-run the step-3 resolver check; diff observed vs manifest |
| `connection to server at "127.0.0.1", port 5432 failed` | `--database-url` not passed; defaulted to localhost | pass `--database-url "$DB"` explicitly |
| `ModuleNotFoundError: No module named 'psycopg2'` | URL scheme was `postgresql://` (selects psycopg2) | use `postgresql+psycopg://...` |
| connection refused / timeout to `*.railway.internal` | used the private URL from an external box | use `DATABASE_PUBLIC_URL` (`*.proxy.rlwy.net`) |
| TLS/SSL errors against Railway | missing sslmode | append `?sslmode=require` |
| `$SF` empty inside `python - <<PY` | var set but not exported | `export SF` before the heredoc |
| `engine failure on <move> at fen <fen>: engine read deadline exceeded`; manifest `status: failed` with large `unprocessed` | one search exceeded 300s under load and aborted all workers | re-run the same command — resume skips the already-stored rows; if the *same* FEN times out every run, that position is pathological (investigate it or raise `SEARCH_DEADLINE_S`) |
