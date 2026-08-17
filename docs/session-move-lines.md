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
  "client_request_id": "uuid",
  "line_revision": 2,
  "after_ply": 7
}
```

Under the session's `FOR NO KEY UPDATE` lock, the endpoint compares the
revision, deletes the tail, advances the revision, and writes
`session_move_truncation_receipt` in one transaction. The receipt is unique by
both `(session_id, client_request_id)` and `(session_id, to_revision)`. Retrying
the exact request ID and body returns its recorded result without deleting or
advancing again; reusing the ID with another body is a conflict. Evidence is
bumped and recomputation requested only when a deleted row belongs to a session
that is already evidence-eligible.

The success response records the linearized operation:

```json
{
  "client_request_id": "uuid",
  "from_revision": 2,
  "to_revision": 3,
  "line_revision": 3,
  "after_ply": 7,
  "deleted_move_count": 4,
  "evidence_changed": false
}
```

## Browser race ordering

The analysis coordinator rewinds optimistically. Before the board changes, it
synchronously creates a new line epoch, pauses ordinary uploads, aborts the old
batch/retry timer, prunes reverted analysis, and carries every analyzed
surviving ply that is not known committed into the replacement upload state.
Callbacks from an older epoch cannot mark rows uploaded or schedule retries.

Truncation requests form one serial chain. Rapid takebacks only reduce the
desired retained ply; the next request is sent after the current head is
acknowledged and uses that acknowledgement's revision. A transient failure
retries the same request ID and body. The typed 409
`FOREIGN_BRANCH_REVISION` and `MOVE_LINE_IDENTITY_CONFLICT` responses halt move
uploads as permanent identity conflicts. On the truncation request itself, any
other 4xx except 408/429 is also terminal for automatic retry and surfaces the
local `line_sync_conflict` diagnostic; the player may explicitly retry the
retained idempotent request. This broader truncation rule covers terminal-state,
authorization, validation, and idempotency rejections that cannot heal on a
timer. Ordinary move uploads keep the narrower rule: unrelated 4xx responses
retain their existing retry path and do not surface line-sync copy. Re-sending
an unchanged payload cannot repair either identity conflict, so active play
offers **Start new game** rather than retry. A locally retained truncation whose
acknowledgement is internally inconsistent also uses `line_sync_conflict` and
may retry that exact request ID and body. There is no cross-tab leader or merge
protocol; a second stale tab is intentionally stalled until the player starts a
new session.

## Complete-line proof and terminal deadline

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
`POST /api/drills/{id}/fail` does not receive or persist a PGN, so that cohort
neither runs terminal reconciliation nor sets its marker and therefore skips the
PGN-backed exact-length proof; it still uses the pre-existing row/FEN
reconstruction checks. This is an explicit limitation, not a fail-closed claim.

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

Terminalization retains one 4,000 ms absolute deadline. Penultimate-analysis
settling has first claim on a shared 300 ms pre-upload window; the already
running truncation chain may block only for what remains. A synchronized chain
uses its acknowledged revision. A permanent conflict returns immediately, and
an unresolved chain at the subdeadline is detached as `deadline_expired`.
Detachment cancels local retries and callback ownership but does not abort an
already-sent truncation request, which may still commit server-side.
Final-full then receives the remaining global budget—nominally at least about
3,700 ms—and the game or drill terminal request proceeds even if that upload
receives a revision 409. The client records `synchronized`, `deadline_expired`,
or `permanent_conflict` in terminal telemetry and, when final-full commits, in
`session_upload_receipt.line_sync_verdict`.

## Authorities

- Schema: `backend/app/models.py` and Alembic revision `20260814_01`.
- API and proof: `backend/app/api/session.py`, `backend/app/game_phase.py`,
  `backend/app/terminal_pgn.py`, `backend/app/ply_coordinates.py`, and the SQL
  companion in `backend/app/session_contracts.py`.
- Historical rollout census: `backend/app/opening_line_proof_audit.py` and
  `backend/scripts/audit_opening_line_proof.py`; guarded repair:
  `backend/app/opening_line_proof_backfill.py` and
  `backend/scripts/repair_opening_line_proof_rows.py`.
- Browser chain and deadline: `src/services/GameAnalysisCoordinator.ts` and
  `src/hooks/useChessGameLifecycle.ts`.
- Behavioral gates: `backend/test_session_move_line*.py`, coordinator/lifecycle
  tests, and `src/utils/api.test.ts`.
