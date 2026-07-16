# Session Accuracy Algorithm Versioning

This document is the durable policy for changing the persisted whole-game
accuracy algorithm after the initial cache rollout.

## Stored contract

Each ended, visible game session stores:

- `player_accuracy`: the rounded `0..100` integer, or `NULL` when inputs do
  not support a result;
- `player_accuracy_algo_version`: the algorithm version that attempted the
  computation, including computations whose legitimate result is `NULL`.

Aggregate reads exclude `NULL` accuracy values. They do not use the version
column and do not retry computation from a GET request.

Every backfill selection and guarded update includes both missing and stale
versions:

```sql
player_accuracy_algo_version IS NULL
OR player_accuracy_algo_version < :current_version
```

## Frozen implementation

Each version has an immutable module such as `app/accuracy_v1.py` or
`app/accuracy_vN.py`. Historical migrations import their matching frozen
module,
never the mutable `app/accuracy.py` re-export.

`app/accuracy.py` owns:

- `ACCURACY_ALGO_VERSION`;
- `CHESS_VERSION_PIN`;
- public re-exports for the current frozen module;
- `accuracy_for_sessions(db, sessions)`, the live-versus-cache read seam;
- `game_accuracy_for_rows(...)`, the guarded entry point every live caller uses
  instead of `compute_game_accuracy` (see below).

Do not edit a frozen module in place after its values have been persisted.
Create the next module and advance the version instead.

### The input contract is frozen too

`app/accuracy_rows_v1.py` is a frozen module on this same contract. It is not an
algorithm — it is the *input-shape* contract in front of one:
`compute_game_accuracy` attributes each ply to a mover by index PARITY but takes
the eval's sign from `move.color`. Those are independent axes, so a row list that
is not the contiguous mainline ply-coordinate grid makes them disagree and the
returned accuracy is silently WRONG rather than `NULL`.

It is frozen for the same reason the algorithm is: a persisted `player_accuracy`
depends on whether this validation passed. Migrations therefore import
`app.accuracy_rows_v1` **directly**, never the `app.accuracy` re-export — a guard
that only wrapped the live surface would be skipped by exactly the code that
writes most of the rows.

A validation failure stores `NULL` and still stamps the version: v1 *attempted*
the computation and its input contract rejected the inputs, which is what a
stamped `NULL` means in the stored contract above. Do not bump
`ACCURACY_ALGO_VERSION` for a change to the input contract — the algorithm did
not change. Supersede with an `accuracy_rows_v2.py` if the contract itself must
change.

## Python-chess policy

Installed `python-chess` behavior is part of the accuracy algorithm because
PGN parsing, SAN legality, and mainline traversal affect the computed inputs.

Any chess dependency pin change is an accuracy algorithm-version change. This
rule applies even when the upgrade appears parse-compatible and all golden
fixtures pass. The fixture matrix is necessary regression coverage, not proof
over every stored PGN.

The version-pin test must assert both:

```python
ACCURACY_ALGO_VERSION == expected_algorithm_version
chess.__version__ == CHESS_VERSION_PIN
```

A fresh replay of old migrations may compute old-version rows using the newly
installed parser. This is acceptable only because the migration history must
finish with a current-version re-backfill that converges every ended, visible
row to the current algorithm.

## Why steady-state changes use three releases

New algorithm hooks cannot protect rows before their deployment becomes
active. A same-deploy version bump, backfill, and cache-only read switch
recreates the original write gap.

There is also a rollback hazard. Once new hooks write version N, a predecessor
that reads a version-agnostic cache would average a mixture of version N and
version N-1 results.

The rollback-safe sequence therefore has three separately deployed releases.

### vN.0: prepare live reads

- Change only `accuracy_for_sessions` to compute live using the current
  version N-1 frozen implementation.
- Do not change hooks, version constants, or migrations.
- Verify stats and history no longer consume cached accuracy.
- Rollback to vN-1.2 is safe because no version N rows exist yet.

### vN.1: activate version N writers

- Add the frozen `app/accuracy_vN.py` module.
- Set `ACCURACY_ALGO_VERSION = N` and update the public re-exports.
- Update `CHESS_VERSION_PIN` when the change includes a chess upgrade.
- Make both write hooks stamp version N.
- Keep aggregate reads live through the seam.
- Ship no re-backfill migration.

The cache is temporarily a mixture of versions, but no aggregate reads it.
Rollback to vN.0 is safe because vN.0 also reads live.

### vN.2: converge and restore cache reads

- Deploy only after vN.1 is the only writer serving production.
- Apply the release's non-vacuous production stamp gate.
- Run a new idempotent migration that re-backfills every ended, visible row
  selected by the missing-or-stale version predicate.
- Use a guarded update so the migration cannot clobber a fresher hook write.
- Stamp version N even when the computed accuracy is legitimately `NULL`.
- Fail closed unless zero ended, visible rows remain unstamped or stale.
- Restore `accuracy_for_sessions` to cache-only only after that assertion.

Rollback from vN.2 uses a forward-revert artifact shaped like vN.1 while it
retains the vN.2 migration file. See
[`migration-deploy-runbook.md`](migration-deploy-runbook.md).

## Initial v1 exception

The initial v1 rollout needs only two releases because its predecessors
compute accuracy live:

- Release A adds version-1 hooks while reads remain live.
- Release B backfills version 1 and switches reads to the cache.

Release A can revert to pre-cache code without consuming mixed cached values.
Release B can revert to Release-A-shaped code that computes live. Do not use
this exception as the template for later algorithm versions.

## Removing hooks after cache-only reads ship

Do not remove the accuracy write hooks while keeping or later restoring
cache-only reads without a new repair migration. Once the original backfill
revision is stamped, Alembic will not rerun it. Games ending during a hookless
window would remain unstamped and silently disappear from aggregate accuracy.

Only two paths are valid:

1. switch to live reads and remain on live reads while hooks are absent; or
2. restore hooks everywhere, pass the production writer gate, run a new
   idempotent repair migration with the guarded backfill and fail-closed
   assertion, and only then restore cache-only reads.

All rollbacks across migration heads follow the forward-revert runbook.
