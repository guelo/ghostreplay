# Session move-line revisions

Session move rows form one canonical branch per game session. Active unrated
takebacks update that durable branch through a server-owned revision rather
than treating the browser rewind as a purely local edit. Rated takebacks still
end the rated game first, and practice play after an ended game remains local.

## Coordinates and persistence

`ply_after(move_number, color)` is the canonical absolute coordinate in
`backend/app/ply_coordinates.py`: White at fullmove `m` is `2m - 1` and Black
is `2m`. A truncation with `after_ply = N` retains exactly rows whose coordinate
is at most `N`; browser history indices `0..N-1` describe those same plies.

`game_sessions.move_line_revision` starts at zero and advances once for every
accepted truncation, including an empty deletion. The browser sends the current
revision as `line_revision` on incremental, final-full, rated-revert, and late
evaluation-repair uploads. The server accepts an omitted revision only while
the live revision is zero, for mixed-version rollout. A stale or missing token
after the first truncation returns HTTP 409 with:

```json
{
  "error_code": "FOREIGN_BRANCH_REVISION",
  "current_revision": 3
}
```

The response is diagnostic, not authority: a stale tab must not adopt that
revision and begin writing onto a branch it did not create.

Within a versioned branch, `(move_number, color, move_san, normalized
fen_before, normalized fen_after)` is immutable. Evaluation, classification,
best-line, and provenance fields may still enrich an existing row. A changed
line must be truncated before replacement moves are uploaded.

## Truncation wire contract

`POST /api/session/{session_id}/moves/truncate` is available only to the owning
user for an active unrated session. Its request is:

```json
{
  "line_revision": 2,
  "after_ply": 7
}
```

Under the session's `FOR NO KEY UPDATE` lock, the endpoint compares the
revision, deletes the tail, and advances the revision in one transaction. The
advance happens even when no rows matched: it is the durable fence that makes
every already-sent upload carrying revision 2 stale after this transaction.
Evidence is bumped and recomputation requested only when a deleted row belongs
to a session that is already evidence-eligible.

The request is intentionally one-shot. There is no truncation receipt or
idempotency key. If the browser does not receive a clean acknowledgement, it
does not know whether the generation advanced and must not retry the deletion
after replacement play may have accumulated.

The success response returns only the newly acknowledged generation:

```json
{
  "line_revision": 3
}
```

## Browser race ordering

The takeback control subscribes to the coordinator's transition-availability
guard. It is enabled only while the current session owns a writable,
synchronized upload epoch; a stopped accuracy-failed drill and a fail-closed
epoch therefore cannot offer a takeback that would become a local-only rewind.
Once that shared guard accepts the transition, the coordinator rewinds
optimistically. Before the board changes, it synchronously creates a new line
epoch, pauses ordinary uploads, aborts the old batch/retry timer, prunes
reverted analysis, and carries every analyzed surviving ply that is not known
committed into the replacement upload state. Callbacks from an older epoch
cannot mark rows uploaded or schedule retries.

Only one takeback transition may be in flight. Local play and analysis may
continue behind the paused upload state, but the takeback control remains
disabled until that request succeeds. A clean response exactly one revision
ahead installs the new revision, re-enables uploads, and uploads surviving plus
newly analyzed dirty plies. A timeout, network failure, conflict, or malformed
acknowledgement keeps the revision unknown and leaves uploads and takebacks
disabled for that session. The only recovery offered is reload or **Start new
game**; there is no automatic retry, response-negotiation chain, cross-tab
leader, revision adoption, or branch merge protocol.

Typed `FOREIGN_BRANCH_REVISION` and `MOVE_LINE_IDENTITY_CONFLICT` responses from
ordinary move writers use the same fail-closed diagnostic. Unrelated upload
failures retain their existing bounded retry behavior.

## Terminal behavior while the generation is unknown

Every current terminal route carries the browser's last acknowledged line
revision. This includes game end/resign/abandon and drill fail/natural-end/
abandon. A terminal call never waits for a takeback request. If the browser
reports an unknown transition, or its supplied revision does not match under
the session row lock, the terminal transaction deletes all `session_moves`,
advances `move_line_revision`, leaves `terminal_line_reconciled` false, and then
completes the terminal state change. This deliberately loses branch-dependent
evidence but prevents a stale row set from becoming score-visible. Any older
upload or truncation that reaches the lock later is rejected by the generation
or active-session checks.

For mixed-version clients, omitting the terminal revision is accepted as an
acknowledgement only while the server revision remains zero. Omission after a
takeback is treated as unknown and follows the same discard path.

## Complete-line proof and terminal upload

A versioned `final_full` upload is checked as an exact standard-start line:

1. row count equals the request's terminal ply;
2. coordinates are exactly `1..N`;
3. the first pre-move FEN is the standard starting position; and
4. every SAN is legal and every before/after position is continuous.

The typed verdict is `passed`, `wrong_row_count`, `coordinate_mismatch`,
`nonstandard_start`, or `illegal_or_discontinuous_line`. This proof is advisory
for persistence: otherwise-valid rows, accuracy, and the durable final-upload
receipt still commit and the endpoint returns 200. A failed verdict suppresses
the evidence cursor bump and graph/opportunity/cache/recompute side effects.
Fresh opening-evidence replay applies the same proof independently when the
terminal writer has set `game_sessions.terminal_line_reconciled` and the bounded
replay of `game_sessions.pgn` is known. Its `N` is the length returned by the
same byte-ceiling and PGN-mainline replay authority used by terminal row
reconciliation. Null, unparseable, and size-over-ceiling PGNs skip this extra
proof; rows surplus to a shorter PGN retain the measured fuller-record policy.
An exact or short row set with a known PGN is proof-checked and an unproven line
is excluded. Historical sessions default the marker false because no terminal
reconcile boundary covered them; they retain the pre-proof replay behavior on
the `raw-v8` cache miss rather than silently changing a user's score. Both the
database probe and fetched-row replay-cache keys fold the PGN and marker, so a
proof-input change cannot reuse an earlier verdict. The slower raw evidence
digest emits one fixed-width hash of those inputs per session rather than
repeating the full PGN on every move-row line.

Accuracy-failed drills are the one evidence-eligible state that remains active.
`POST /api/drills/{id}/fail` receives the line revision but does not receive or
persist a PGN, so an acknowledged cohort neither runs terminal reconciliation
nor sets its marker and therefore skips the PGN-backed exact-length proof; it
still uses the pre-existing row/FEN reconstruction checks. An unknown or stale
generation discards its rows before the state becomes evidence-eligible.

Both `/api/game/end` and drill `/natural-end` reconcile a verified missing or
sparse PGN tail in the terminal transaction before the session becomes opening-
evidence eligible.

`raw-v8` invalidates historical replay-cache entries, while the false default on
`terminal_line_reconciled` preserves those sessions' evidence admission. The
aggregate-only, read-only census below quantifies how many legacy sessions would
have shifted without that compatibility marker and identifies candidates that
can be opted into the proof after verified repair:

```bash
cd backend
source .venv/bin/activate
python scripts/audit_opening_line_proof.py
```

`proof_short_sessions` counts evidence-visible sessions whose filtered move rows
are shorter than a bounded PGN. `physical_row_short_sessions` isolates genuinely
missing rows; `null_fen_short_sessions` identifies sessions shortened by the
replay query's `fen_before IS NOT NULL` boundary. The audit makes no repairs and
prints no row identifiers or contents.

For physically missing rows, the companion repair is dry-run-first and only
plans sessions whose existing rows match their declared PGN coordinates and
identity through the serving-path sparse reconciler:

```bash
python scripts/repair_opening_line_proof_rows.py
# after reviewing the aggregate plan:
python scripts/repair_opening_line_proof_rows.py --apply
```

Apply rechecks every candidate under the session writer lock, derives only the
verified PGN rows, recomputes cached accuracy, bumps the evidence cursor, and
commits one session at a time. Repaired sessions set the reconciliation marker
and opt into the proof. `unrepairable_physical_short_sessions` and any
`null_fen_short_sessions` remain on the historical compatibility path; the
repair does not guess, rewrite, or newly exclude them.

The audit and repair dry-run intentionally work against the pre-20260814_01
schema. The optional `--apply` phase uses the current ORM and therefore runs
after that schema migration.

Terminal final-full upload retains one 4,000 ms absolute deadline, with the
existing 300 ms analysis-settling window counted inside it. Branch
synchronization consumes none of that budget. If the revision is acknowledged,
the final upload uses it and the terminal route receives the same value. If the
revision is unknown, the client skips settling, final-full upload, and late
evaluation repair, calls the terminal route immediately with an explicit
discard flag, and lets the server-side terminal fence suppress the evidence.

## Authorities

- Schema: `backend/app/models.py` and Alembic revision `20260814_01`.
- API and proof: `backend/app/api/session.py`, `backend/app/game_phase.py`,
  `backend/app/terminal_pgn.py`, `backend/app/ply_coordinates.py`, and the SQL
  companion in `backend/app/session_contracts.py`.
- Historical rollout census: `backend/app/opening_line_proof_audit.py` and
  `backend/scripts/audit_opening_line_proof.py`; guarded repair:
  `backend/app/opening_line_proof_backfill.py` and
  `backend/scripts/repair_opening_line_proof_rows.py`.
- Browser epoch and terminal handling:
  `src/services/GameAnalysisCoordinator.ts` and
  `src/hooks/useChessGameLifecycle.ts`.
- Behavioral gates: `backend/test_session_move_line*.py`, coordinator/lifecycle
  tests, and `src/utils/api.test.ts`.
