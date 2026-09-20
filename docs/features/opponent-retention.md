# Opponent replay deadline

`OPPONENT_DECISION_RETENTION_ENABLED` defaults to `0`; only `1` enables enforcement.
Any other value is rejected at startup. Both retention settings are validated
before database access or background workers start, including the duration when
enforcement is disabled.
`OPPONENT_DECISION_RETENTION_SECONDS` is unset by default. Setting it to an
explicitly chosen positive integer selects R and initializes new sessions, even
while enforcement remains disabled. There is no production default duration;
enforcement without a selected duration is an invariant error.
Changing R never changes an existing deadline. Both session
creation routes initialize from the persisted `started_at` in their creation
transaction, including database-default starts. Existing start/end clocks and
scoring clock interfaces are unchanged.

The rollout in `g-decision-rollout` owns the aggregate census, product selection
of R, coordinated legacy initialization, reader switch and activation. Finish the
final dual-writer drain, targeting-fact backfill and coverage verification BEFORE
initializing any deadlines: backfill refuses even future non-NULL deadlines.
Deploy this implementation with enforcement disabled and the duration unset.
After the backfill checkpoint, configure the selected duration on every creator,
drain the old writers, initialize legacy deadlines, and verify none remain NULL
before enabling enforcement. During activation, coordinate writers so no
NULL-deadline session can be admitted after enforcement turns on;
a missing deadline with enforcement enabled is an invariant error, not expiry.

Legacy SQL is in
[`initialize_opponent_decision_deadlines.sql`](../../backend/scripts/initialize_opponent_decision_deadlines.sql).
Execute it in a transaction with the selected positive `retention_seconds` bind
parameter as part of that coordinated rollout. It fills only NULL deadlines from
historical starts and is safe to repeat without extending initialized sessions.
This implementation does not run it against production or enable deletion.

The opponent endpoint checks the primary database's fresh `clock_timestamp()`
before replay. Publication uses one materialized clock sample for both
`INSERT ... SELECT` admission and the envelope's `served_at`. Winning targeting
facts receive exactly that returned timestamp. A rejected insert is 410; a
fingerprint loser commits and reselects the winner, retaining 503 if cleanup
deleted the winner first. Normal replay and record acquire no session write lock.

Drill route admission precedes proof reads. Existing session mutation locks
remain in place; their fresh expiry rechecks precede root/boundary writes and
off-route failure. Proof reads stay unlocked. A deleted opponent proof gives 422
before mutation; a deleted player anchor soft-declines proof, then expiry under
the mutation lock gives 410. The next admission gives 410 in either case.
Already-stored root boundary results survive envelope deletion.

The HTTP envelope uses `error.code=http_410` and
`error.details.error_code=OPPONENT_SESSION_EXPIRED`. Normal/converted play uses
local fallback with no decision ID or target; active/root-reached drills stop
with their board and confirmation ownership intact. See [drill mode](drill-mode.md).
History, finalization, uploads and earned reviews keep their existing contracts.

`backend/test_opponent_session_expiry.py` covers deterministic deletion barriers,
actual PostgreSQL mutation-lock waits, unlocked replay/record, and fresh database
time. These tests are registered in the required PostgreSQL gate. SQLite tests
cover functional behavior only. The cleanup child owns the maintenance job,
deletion margin, release qualification and operational runbook.
