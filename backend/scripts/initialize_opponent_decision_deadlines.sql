-- Rollout only: finish final fact backfill + coverage verification FIRST.
-- Run within a transaction, binding the selected positive retention_seconds.
-- Coordinate session creators and activation; this is not a serialization gate.
-- Existing deadlines and historical session start/end times are never changed.
UPDATE game_sessions
SET opponent_decisions_expires_at =
    started_at + make_interval(secs => :retention_seconds)
WHERE opponent_decisions_expires_at IS NULL;
