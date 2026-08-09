"""PostgreSQL test gate + fixtures (g-accuracy-schema).

Importable pytest plugin that owns everything PostgreSQL-backed tests need:

- the ``pg_gate`` marker (aliased as ``pg_required``) and its skip/fail gate,
- the fixed ``REQUIRED_PG_GATE_TESTS`` / ``REQUIRED_PG_GATE_PARAM_CASES``
  manifests plus the required-mode collection guards and skip-promotion
  hookwrapper that make the gate fail closed rather than pass with missing
  coverage,
- the shared migrated-schema fixtures (``pg_engine`` / ``pg_session_factory`` /
  ``pg_client``) moved out of ``conftest.py``, and
- ``pg_migration_db``, a disposable-database fixture for migration tests that
  need to upgrade a fresh database from base.

All environment reads happen at fixture / collection call time (never frozen at
import) so a test can monkeypatch the relevant variables and the gate reacts.

Gate policy (see ``_pg_url`` / ``_require_pg``):

- Developer default (no URL, ``GHOSTREPLAY_REQUIRE_PG_TESTS`` unset): PG-backed
  tests SKIP cleanly.
- Required mode (``GHOSTREPLAY_REQUIRE_PG_TESTS=1``): a missing URL FAILS instead
  of skipping, so CI cannot silently drop PostgreSQL coverage.

``conftest.py`` activates this via ``pytest_plugins`` and re-exports
``pg_required`` / ``pg_gate`` so ``from conftest import pg_required`` keeps
working.

The required PostgreSQL gate command (CI and the release rehearsal) is::

    GHOSTREPLAY_REQUIRE_PG_TESTS=1 \\
    GHOSTREPLAY_TEST_PG_URL="postgresql://.../ghostreplay_test" \\
    GHOSTREPLAY_TEST_PG_MAINT_URL="postgresql://.../postgres" \\
    pytest -m pg_gate --strict-markers -rs
"""

from __future__ import annotations

import os
import pathlib
import re
import time
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Environment reads (always call-time, never module-level constants).
# ---------------------------------------------------------------------------


# How long ``_pg_pool_leak_guard`` waits for an in-flight connection to find its way
# back to the pool before calling it stranded. Generous enough that a thread finishing
# its return path is never mistaken for a leak, short enough that a real leak is
# reported against the test that caused it.
_POOL_DRAIN_GRACE_SECONDS = 5.0

# Ceiling on the per-test all-table TRUNCATE's wait for its ACCESS EXCLUSIVE locks.
# Nothing the suite itself owns is running when it fires, so waiting longer only
# converts an attributable failure into a hung run.
_TRUNCATE_LOCK_TIMEOUT = "20s"

# The live ``pg_engine`` for this run, published by the fixture so the autouse pool
# leak guard can read it WITHOUT requesting the fixture (which would build an engine —
# or skip — for tests that never wanted one). ``None`` whenever no engine exists.
_LIVE_PG_ENGINE: dict[str, object] = {"engine": None}


def _pg_url() -> str | None:
    """URL of the shared PostgreSQL test database, or None when unset."""
    return os.getenv("GHOSTREPLAY_TEST_PG_URL") or os.getenv("TEST_DATABASE_URL_PG")


def _pg_maint_url() -> str | None:
    """Maintenance URL used ONLY to CREATE/DROP disposable databases.

    Deliberately separate from the app/test URL: authority to create and drop
    databases must come from an explicitly-provisioned maintenance connection,
    never from the connection the tests run their queries on.
    """
    return os.getenv("GHOSTREPLAY_TEST_PG_MAINT_URL")


def _require_pg() -> bool:
    """True when missing PostgreSQL URLs must FAIL rather than skip."""
    return os.getenv("GHOSTREPLAY_REQUIRE_PG_TESTS") == "1"


# ---------------------------------------------------------------------------
# Marker + gate.
#
# ``pg_gate`` is the Release-A PostgreSQL gate marker: it identifies exactly the
# migration and concurrency proofs that the required-mode CI command
# (``GHOSTREPLAY_REQUIRE_PG_TESTS=1 pytest -m pg_gate``) must run against a real
# PostgreSQL. ``pg_required`` is a backward-compatible alias so the many
# ``from conftest import pg_required`` / ``@pg_required`` call sites keep applying
# this same marker object. The pre-existing analysis-cache and position-analysis
# PostgreSQL suites deliberately do NOT use this marker (they define their own
# module-local ``skipif``), so they stay out of the Release-A gate and keep their
# own skip behaviour.
# ---------------------------------------------------------------------------

pg_gate = pytest.mark.pg_gate
pg_required = pg_gate  # alias: both apply the pg_gate marker object.


# Fixed manifest of every Release-A PostgreSQL gate test, keyed by node identity
# (``path::function`` with any parametrization stripped). In required mode the
# collection guard fails hard if any identity here is absent from the gated
# selection, so a deleted / renamed / accidentally-unmarked invariant cannot
# silently drop out of CI coverage. Keep in lockstep with the ``@pg_gate``
# decorations across the Release-A test files.
REQUIRED_PG_GATE_TESTS = frozenset({
    # game-end / post-end /moves cached-accuracy write hooks (g-accuracy-hooks)
    "test_accuracy_hooks.py::test_pg_game_end_first_then_late_moves_heals",
    "test_accuracy_hooks.py::test_pg_game_end_lock_serializes_concurrent_late_moves",
    "test_accuracy_hooks.py::test_pg_moves_first_then_game_end_sees_committed_inputs",
    "test_accuracy_hooks.py::test_pg_moves_lock_serializes_concurrent_game_end",
    # checkmate final-ply eval backfill: Phase A REPEATABLE READ read-only snapshot,
    # parent-session FOR NO KEY UPDATE lock, and cached-accuracy recompute on the
    # migrated schema (g-eh2w data repair for g-hs78)
    "test_backfill_checkmate_final_ply_evals.py::test_pg_run_recomputes_accuracy_and_bumps_under_real_locks",
    # draw final-ply eval backfill: same Phase A REPEATABLE READ read-only snapshot,
    # parent-session FOR NO KEY UPDATE lock, and cached-accuracy recompute on the
    # migrated schema (g-c60b data repair, sibling of the checkmate backfill above)
    "test_backfill_draw_final_ply_evals.py::test_pg_run_recomputes_accuracy_and_bumps_under_real_locks",
    # blunder NKU idempotency (g-writer-locks)
    "test_blunder_api.py::test_record_blunder_concurrent_same_key_records_once",
    # advisory lock before the first graph write + cursor-is-last on the
    # first-blunder path (g-n6c2). Postgres-only by necessity: the lock is a no-op
    # off Postgres, so the SQLite suite is blind to its position.
    "test_blunder_api.py::test_pg_blunder_advisory_lock_precedes_writes_and_cursor_is_last",
    # Avg CPL aggregates reach round_half_up_cpl as a Decimal, un-cast (g-22t8.5).
    # SQLite's AVG already returns a float, so this cast guard only bites on the real
    # dialect — it is the one check a float() regression cannot pass.
    "test_centipawn_loss.py::test_pg_cpl_aggregates_reach_helper_as_decimal",
    # branch-scoped route / next-opponent stale-write locks (g-branch-locks)
    "test_branch_locks.py::test_next_opponent_releases_lock_before_engine_so_moves_commits",
    "test_branch_locks.py::test_next_opponent_stale_converted_falls_through",
    "test_branch_locks.py::test_next_opponent_stale_failed_returns_400",
    "test_branch_locks.py::test_route_check_off_route_yields_to_concurrent_root_reached",
    "test_branch_locks.py::test_route_check_root_reached_snapshot_preserves_concurrent_failure",
    "test_branch_locks.py::test_route_check_target_reached_yields_to_concurrent_failure",
    # write-once drill evidence boundary under real row-lock contention
    # (g-root-confirm-api). The single-connection SQLite engine cannot stage the race
    # that decides which of two confirmations stamps the ply.
    "test_drill_root_confirmation.py::test_concurrent_confirmations_converge_on_one_ply",
    # per-user graph-write advisory lock (g-graph-lock)
    "test_graph_write_lock.py::test_recording_times_out_and_persists_nothing_when_lock_held",
    "test_graph_write_lock.py::test_recording_vs_recording_serialize",
    "test_graph_write_lock.py::test_reverted_lock_reproduces_opposite_order_deadlock",
    "test_graph_write_lock.py::test_worker_vs_recording_serialize",
    # rated game-end users-row lock + games_played-first durable head (g-rating-serial)
    "test_rating_serialize.py::test_concurrent_double_end_one_session_loser_gets_400",
    "test_rating_serialize.py::test_cursor_writer_completes_while_end_paused_in_rating",
    "test_rating_serialize.py::test_same_user_distinct_session_ends_chain_cleanly",
    "test_rating_serialize.py::test_users_lock_prevents_lost_games_played_update",
    # session /moves shared-graph advisory serialization (g-graph-lock)
    "test_session_graph_lock.py::test_moves_concurrent_same_opening_serialize",
    "test_session_graph_lock.py::test_moves_does_not_block_on_held_lock_production_shape",
    "test_session_graph_lock.py::test_moves_graph_lock_retry_succeeds",
    "test_session_graph_lock.py::test_moves_graph_lock_timeout_degrades",
    # SRS review NKU idempotency (g-writer-locks)
    "test_srs_api.py::test_srs_review_concurrent_same_key_single_row",
    # SRS/moves cross-root deadlock matrix (g-writer-locks); param cases pinned below
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix",
    # Release-A schema migration on a disposable PostgreSQL DB (g-accuracy-schema)
    "test_release_a_migrations.py::test_pg_disposable_release_a_migration",
    # Release-B backfill/repair correctness core (g-b-backfill-core). The
    # NOT VALID -> validated CHECK transition, the frozen visibility predicate's
    # parity with app/session_contracts.py, and the ply-coordinate detectors'
    # parity with the frozen validator. None of the three is observable from the
    # SQLite migration suite: the CHECK has no NOT VALID state there, the ORM
    # predicate is never exercised against the frozen SQL copy, and
    # PLY_DETECTOR_ONE_PG differs from its SQLite twin by exactly the
    # CAST(:sid AS uuid) that fails when the bind arrives as text.
    "test_release_b_pg_matrix.py::test_pg_release_b_validates_the_not_valid_check",
    "test_release_b_pg_matrix.py::test_pg_population_parity_matrix",
    # The guarded update's typed arrays across an all-NULL / mixed / all-scored
    # batch, plus the one-server-statement property. SQLite's guarded update is a
    # per-row statement with no arrays, so it cannot express any of this.
    "test_release_b_pg_matrix.py::"
    "test_pg_guarded_update_typed_arrays_all_null_mixed_and_all_scored",
    # The nil UUID is a schema-valid session ID; a sentinel-cursor keyset sweep
    # would skip it and then fail to converge.
    "test_release_b_pg_matrix.py::test_pg_nil_uuid_session_is_backfilled",
    "test_release_b_pg_matrix.py::test_pg_detector_parity",
    "test_release_b_pg_matrix.py::test_pg_integer_division_floors_like_the_validator",
    # The sizing harness's empty teardown point. Only reachable on PostgreSQL:
    # the failure needs PLY_DETECTOR_SQL over uuid session ids against a row that
    # already carries a served value on a broken grid.
    "test_release_b_pg_matrix.py::"
    "test_pg_synthesize_stamped_empties_both_populations_with_broken_grids_present",
    # The two dimension readings the synthesis moves between
    # (g-b-size-harness-defects). PostgreSQL-only for the same reason: the
    # displacement is produced by synthesize_repair, whose candidate selection and
    # ply deletion run through PLY_DETECTOR_SQL over uuid session ids.
    "test_release_b_pg_matrix.py::test_pg_the_harness_records_the_reading_its_synthesis_moved",
    # Release-B runtime envelope (g-b-runtime-envelope). Every one of these is
    # structurally invisible to the SQLite suite: SQLite has no statement_timeout
    # and no lock_timeout, no FOR NO KEY UPDATE SKIP LOCKED, no second writer to
    # stall, no pg_stat_activity/pg_locks to observe, and no per-batch transactions
    # on an independent connection.
    #
    # The VALIDATE lock-timeout leak (SET LOCAL is transaction-scoped and env.py
    # opens one transaction around the whole run).
    "test_release_b_pg_runtime.py::test_pg_validate_lock_timeout_does_not_leak_into_the_row_locks",
    # The arming rule, in both modes: the armed value is the LEAST of every budget
    # the statement spends from, and a scan never receives a batch allowance.
    "test_release_b_pg_runtime.py::"
    "test_pg_per_batch_arming_is_non_increasing_and_never_a_fresh_batch_allowance",
    "test_release_b_pg_runtime.py::"
    "test_pg_atomic_arming_draws_on_the_residual_stall_budget_from_the_first_lock",
    "test_release_b_pg_runtime.py::"
    "test_pg_the_materialization_and_the_assertions_never_get_a_batch_allowance",
    # Scan accounting: the per-path counts under lock, the zero-population path, the
    # already-stamped boundary, and one scan per PASS rather than per batch.
    "test_release_b_pg_runtime.py::"
    "test_pg_atomic_scans_under_lock_is_a_bound_and_each_path_is_exact",
    "test_release_b_pg_runtime.py::test_pg_both_populations_zero_takes_no_row_lock_and_scans_twice",
    "test_release_b_pg_runtime.py::test_pg_one_scan_per_pass_not_one_per_batch",
    "test_release_b_pg_runtime.py::"
    "test_pg_backfill_issues_one_selection_sweep_and_one_convergence_count_per_pass",
    # The sweep model (g-b-sweep-batch-cost): the page formula against the
    # runner's actual paging, the frozen pair against a real fixture-scale sweep,
    # and the stale-population postcondition of the row-cloning helper.
    "test_release_b_pg_runtime.py::test_pg_sweep_page_count_matches_the_model",
    "test_release_b_pg_runtime.py::test_pg_frozen_sweep_model_covers_a_live_sweep",
    "test_release_b_pg_runtime.py::"
    "test_pg_synthesize_sessions_establishes_the_stale_population_it_promises",
    # The materialization's transaction contract is MODE-SPLIT, and the one
    # invariant both modes share is that it takes no row lock of its own.
    "test_release_b_pg_runtime.py::"
    "test_pg_materialization_commits_before_the_batch_loop_in_per_batch_mode",
    "test_release_b_pg_runtime.py::"
    "test_pg_materialization_runs_inside_alembics_transaction_in_atomic_mode",
    "test_release_b_pg_runtime.py::"
    "test_pg_materialization_takes_no_row_lock_of_its_own_in_either_mode",
    # The per-batch runner: its own labelled connection with an explicitly closed
    # setup transaction, SKIP LOCKED convergence, the exhaustion template, and
    # durability of committed batches across a failure.
    "test_release_b_pg_runtime.py::"
    "test_pg_per_batch_runner_labels_its_own_connection_and_commits_the_setup_first",
    "test_release_b_pg_runtime.py::"
    "test_pg_skipped_rows_converge_across_passes_and_a_zero_row_pass_is_never_success",
    "test_release_b_pg_runtime.py::"
    "test_pg_a_permanently_locked_row_exhausts_the_pass_bound_with_the_exact_template",
    "test_release_b_pg_runtime.py::test_pg_earlier_batches_stay_durable_when_a_later_one_fails",
    # Enforcement, for slow SQL as well as slow Python, plus budget carry-over.
    "test_release_b_pg_runtime.py::"
    "test_pg_a_slow_statement_is_cancelled_at_its_armed_timeout_and_rolls_back",
    "test_release_b_pg_runtime.py::test_pg_the_guarded_update_inherits_only_what_the_load_left",
    "test_release_b_pg_runtime.py::test_pg_slow_python_compute_rolls_the_batch_back",
    "test_release_b_pg_runtime.py::test_pg_repair_batches_obey_the_same_budget",
    # The observed-lock-hold tripwire is PER-BATCH MODE ONLY. The atomic negative is
    # what forbids the revision from claiming a measurement it cannot make.
    "test_release_b_pg_runtime.py::"
    "test_pg_the_observed_lock_hold_tripwire_fires_after_teardown_and_raises",
    "test_release_b_pg_runtime.py::test_pg_atomic_mode_has_no_lock_hold_tripwire_to_fire",
    # A per-wait cap bounds one wait and never their sum; the residual stall budget
    # bounds the sum. The companion negative is what makes it a proof of a budget.
    "test_release_b_pg_runtime.py::"
    "test_pg_atomic_cumulative_lock_waits_breach_the_residual_budget",
    "test_release_b_pg_runtime.py::"
    "test_pg_the_same_waits_complete_cleanly_under_a_budget_that_absorbs_them",
    # The env.py stall probe: it measures THROUGH the commit, on both paths.
    "test_release_b_pg_runtime.py::test_pg_the_stall_probe_measures_through_the_commit",
    "test_release_b_pg_runtime.py::test_pg_the_stall_probe_still_reports_on_a_failing_upgrade",
    # Relation growth that leaves BOTH populations bit-for-bit unchanged.
    "test_release_b_pg_runtime.py::"
    "test_pg_growth_in_neither_population_still_moves_the_growth_factors",
    # The probe is armed, so its cancellation must reach the frozen template.
    "test_release_b_pg_runtime.py::"
    "test_pg_a_cancelled_dimension_probe_arrives_as_the_frozen_template",
    # The six interleavings, split by mode — live-first is reachable only in atomic
    # mode, because per-batch selection locks at selection time.
    "test_release_b_pg_runtime.py::test_pg_atomic_backfill_yields_to_a_hook_that_wrote_first",
    "test_release_b_pg_runtime.py::"
    "test_pg_per_batch_selection_skips_a_row_a_hook_holds_and_the_hook_wins",
    "test_release_b_pg_runtime.py::"
    "test_pg_per_batch_migration_first_blocks_the_writer_until_the_batch_commits",
    "test_release_b_pg_runtime.py::"
    "test_pg_atomic_repair_re_read_skips_a_grid_the_hook_already_repaired",
    "test_release_b_pg_runtime.py::test_pg_the_repair_nulls_a_broken_row_when_no_hook_intervenes",
    "test_release_b_pg_runtime.py::"
    "test_pg_per_batch_repair_selection_already_holds_the_lock_the_hook_needs",
    # The Phase 3 fixture digest, moved one bound column at a time against a real
    # database (g-b-sizing-harness). The static test beside it says the digest is
    # SCOPED to the revision's input columns; only this one says the SQL built from
    # them actually varies with each — in both directions, so an unbound column may
    # not move it either. PostgreSQL-only by construction: the digest runs over uuid
    # session ids, and the run drops and restores `ck_game_sessions_mode_drill_state`
    # / `ck_game_sessions_drill_rating_boundary` by reading their definitions back
    # from `pg_get_constraintdef`, so that `session_mode` and `drill_state` can each
    # be moved ALONE. Pinned because the digest is the whole expiry rule for the
    # sizing artifacts: uncollected, the fixture claim silently weakens to row counts.
    "test_release_b_sizing.py::"
    "test_the_fixture_digest_moves_for_every_input_and_for_nothing_else",
    # Release-B single-runner guard + stall observation (g-b-runner-guard). The
    # session-scoped two-key advisory guard on a dedicated connection, its survival
    # across 20260709_02's autocommit-block commit, cross-process serialization
    # proven exactly-once by audit counters, the Config.attributes acquisition
    # timeout, fail-safe release, the "Alembic owns the migration transaction"
    # ordering, and the migration connection's in-flight application_name. None is
    # observable from the SQLite suite: SQLite has no advisory locks, no
    # autocommit-block commit to survive, no cross-process migration proxy, and no
    # pg_stat_activity/pg_locks to observe.
    "test_migration_guard.py::test_pg_guard_held_across_the_whole_chain_from_base",
    "test_migration_guard.py::test_pg_seeded_concurrent_backfill_is_applied_exactly_once",
    "test_migration_guard.py::test_pg_acquisition_timeout_raises_concurrent_migration_error",
    "test_migration_guard.py::test_pg_guard_is_released_when_the_migration_fails",
    "test_migration_guard.py::test_pg_alembic_owns_the_migration_transaction",
    "test_migration_guard.py::test_pg_migration_application_name_is_visible_in_flight",
    # Frozen-cohort capture: the consistency fence + atomic publication
    # (g-p4ih-capture). The contract under test IS PostgreSQL REPEATABLE READ
    # snapshot isolation — a concurrent evidence write that the snapshot must not
    # see and the post-snapshot re-read must — so none of it is expressible off
    # the real dialect.
    "test_capture_cohort_pg.py::test_a_killed_lock_holder_releases_immediately_with_no_stale_reap",
    "test_capture_cohort_pg.py::test_a_losing_capture_reads_no_evidence_at_all",
    "test_capture_cohort_pg.py::test_a_rejection_and_a_crash_are_different_diagnostics",
    "test_capture_cohort_pg.py::test_candidate_disappearance_triggers_retry",
    "test_capture_cohort_pg.py::test_candidate_set_appearance_triggers_retry",
    "test_capture_cohort_pg.py::test_concurrent_capture_refused_before_reading",
    "test_capture_cohort_pg.py::test_dialect_refuses_non_postgres",
    "test_capture_cohort_pg.py::test_first_rename_failure_leaves_the_prior_pair_untouched",
    "test_capture_cohort_pg.py::test_guard_pair_movement_triggers_retry",
    "test_capture_cohort_pg.py::test_inter_rename_failure_is_recoverable_mismatched_pair",
    "test_capture_cohort_pg.py::test_missing_output_directory_is_a_typed_refusal",
    "test_capture_cohort_pg.py::test_movement_every_attempt_exhausts_with_no_side_effects",
    "test_capture_cohort_pg.py::test_orphan_replaced_on_rerun",
    "test_capture_cohort_pg.py::test_published_artifact_is_0600",
    "test_capture_cohort_pg.py::test_quiescent_run_completes_first_attempt",
    "test_capture_cohort_pg.py::test_release_guard_shape_at_source_fails",
    "test_capture_cohort_pg.py::test_rerun_is_byte_identical",
    "test_capture_cohort_pg.py::test_retry_then_succeed_on_first_attempt_movement",
    "test_capture_cohort_pg.py::test_self_check_failure_leaves_prior_untouched",
    "test_capture_cohort_pg.py::test_self_check_rejects_a_cohort_the_release_path_would_reject",
    "test_capture_cohort_pg.py::test_self_check_runs_real_scoring_and_publishes",
    "test_capture_cohort_pg.py::test_starvation_regression_global_epoch_only_does_not_retry",
    "test_capture_cohort_pg.py::test_strict_mode_retries_on_global_epoch_movement",
    "test_capture_cohort_pg.py::test_the_child_exits_one_without_a_traceback_on_a_self_check_crash",
    "test_capture_cohort_pg.py::test_the_locks_are_held_across_BOTH_renames",
    "test_capture_cohort_pg.py::test_threshold_crossing_movement_alone_can_exhaust_the_fence",
    "test_capture_cohort_pg.py::test_threshold_crossing_write_to_a_non_captured_pair_triggers_retry",
    "test_capture_cohort_pg.py::test_unavailable_epoch_strict_fails_default_stamps_null",
    # The self-check's unexpected-failure boundary; param cases pinned below.
    "test_capture_cohort_pg.py::test_unexpected_self_check_failures_stay_inside_the_typed_boundary",
    # The full launch from a clean committed clone: fence against a real snapshot,
    # real scoring, both renames, and the reviewable provenance diff.
    "test_capture_end_to_end.py::test_a_crash_between_the_two_renames_fails_closed_and_reruns_clean",
    "test_capture_end_to_end.py::test_a_second_capture_to_the_same_output_is_refused",
    "test_capture_end_to_end.py::test_full_capture_publishes_artifact_and_reviewable_provenance_diff",
    # Replay-cache digest agreement between its two formatters
    # (g-overlay-evidence-reuse). _build_move_rows VALIDATES against the digest
    # _probe_sql computes in the database and STORES the one _session_digest_body
    # (+ the dialect's _body_fold) computes in python; a dialect-specific
    # divergence (integer/timestamp rendering, a collation-dependent ORDER BY, a
    # NULL that string_agg drops instead of rendering as the sentinel, md5
    # disagreeing with hashlib) breaks nothing visibly — it silently turns every
    # warm overlay build back into a full history replay. PostgreSQL is also the
    # only dialect that FOLDS the body server-side, which is what keeps the
    # probe's payload O(sessions); on SQLite the fold is the identity and proves
    # nothing. Pinned, or the whole reuse claim rests on SQLite alone.
    "test_opening_evidence_digest_pg.py::test_md5_fold_pair_agrees_and_keys_match_end_to_end",
    "test_opening_evidence_digest_pg.py::test_null_evals_use_the_sentinel_not_an_empty_field",
    "test_opening_evidence_digest_pg.py::test_probe_ordering_is_collation_independent",
    "test_opening_evidence_digest_pg.py::test_probe_payload_is_fixed_size_per_session",
    "test_opening_evidence_digest_pg.py::"
    "test_real_recompute_persists_l2_across_caller_rollback",
    "test_opening_evidence_digest_pg.py::test_sql_and_python_digest_bodies_are_byte_equal",
    "test_opening_evidence_digest_pg.py::test_warm_rebuild_on_postgres_fetches_no_rows",
})

# The SRS/moves cross-root lock matrix must run all four session/blunder lock
# combinations. Pin the exact bracketed case IDs so silently dropping any row of
# the matrix (e.g. the both-FOR-UPDATE deadlock case) fails the gate rather than
# quietly shrinking it.
REQUIRED_PG_GATE_PARAM_CASES = frozenset({
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix[both_for_update]",
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix[both_nku]",
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix[session_fu_blunder_nku]",
    "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix[session_nku_blunder_fu]",
    # Release B's three-way ply-coordinate detector parity. All five row sets are
    # pinned individually so a case that stops being collected fails the manifest
    # check instead of silently reducing coverage to whatever still runs. The
    # empty set is the case most likely to diverge — count(*) over an empty CTE
    # versus the validator's early return on [].
    "test_release_b_pg_matrix.py::test_pg_detector_parity[well_formed]",
    "test_release_b_pg_matrix.py::test_pg_detector_parity[gap]",
    "test_release_b_pg_matrix.py::test_pg_detector_parity[white_white_adjacency]",
    "test_release_b_pg_matrix.py::test_pg_detector_parity[contiguous_surplus]",
    "test_release_b_pg_matrix.py::test_pg_detector_parity[empty]",
    # ATOMIC_SCANS_UNDER_LOCK is a MAXIMUM over paths, not an identity, so all three
    # reachable atomic paths are pinned individually. The repair-only case is the one
    # most likely to be dropped and the one that matters most: with nothing to
    # back-fill the FIRST row lock is the repair's own, taken AFTER the
    # materialization, so only TWO scans are under lock. A suite that lost it would
    # silently reduce the proof to "3 on every path", which is wrong about where the
    # first lock falls.
    "test_release_b_pg_runtime.py::"
    "test_pg_atomic_scans_under_lock_is_a_bound_and_each_path_is_exact[stale_and_repair]",
    "test_release_b_pg_runtime.py::"
    "test_pg_atomic_scans_under_lock_is_a_bound_and_each_path_is_exact[stale_only]",
    "test_release_b_pg_runtime.py::"
    "test_pg_atomic_scans_under_lock_is_a_bound_and_each_path_is_exact[repair_only]",
    # Each exception the self-check's scoring pass can raise outside the artifact
    # vocabulary is pinned: dropping one would silently narrow the proof that the
    # subcommand still emits a typed diagnostic instead of a traceback.
    "test_capture_cohort_pg.py::"
    "test_unexpected_self_check_failures_stay_inside_the_typed_boundary[boom0]",
    "test_capture_cohort_pg.py::"
    "test_unexpected_self_check_failures_stay_inside_the_typed_boundary[boom1]",
    "test_capture_cohort_pg.py::"
    "test_unexpected_self_check_failures_stay_inside_the_typed_boundary[boom2]",
})


def pytest_configure(config: pytest.Config) -> None:
    # Registering the marker keeps it valid under --strict-markers.
    config.addinivalue_line(
        "markers",
        "pg_gate: Release-A PostgreSQL migration/concurrency proof. Needs "
        "GHOSTREPLAY_TEST_PG_URL; skips in developer-default mode, and under "
        "GHOSTREPLAY_REQUIRE_PG_TESTS=1 a missing URL (or any residual skip) FAILS.",
    )


def _is_gated(item: pytest.Item) -> bool:
    """True for a ``@pg_gate`` (== ``@pg_required``) test."""
    return item.get_closest_marker("pg_gate") is not None


def _gate_identity(nodeid: str) -> str:
    """Function identity of a node id, with any ``[param]`` suffix stripped."""
    return nodeid.split("[", 1)[0]


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Gate ``@pg_gate`` tests on the PostgreSQL URL, at setup time."""
    if not _is_gated(item):
        return
    if _pg_url():
        return
    if _require_pg():
        pytest.fail(
            "GHOSTREPLAY_REQUIRE_PG_TESTS=1 but GHOSTREPLAY_TEST_PG_URL is not set",
            pytrace=False,
        )
    pytest.skip("GHOSTREPLAY_TEST_PG_URL not set; PostgreSQL-backed test skipped")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Required-mode collection guards (no-ops in developer-default mode).

    ``trylast`` so this runs AFTER pytest's own ``-m`` / ``-k`` deselection and
    therefore validates the tests that will actually run. Together these make it
    impossible for the required PostgreSQL gate to pass while silently running
    zero — or an incomplete set of — the Release-A invariants:

    * an empty gated selection is a hard ``UsageError`` (a marker typo or an
      over-narrow ``-k`` would otherwise report "0 selected" and exit green);
    * every identity in ``REQUIRED_PG_GATE_TESTS`` must be collected; and
    * every case in ``REQUIRED_PG_GATE_PARAM_CASES`` must be collected.
    """
    if not _require_pg():
        return
    gated = [item for item in items if _is_gated(item)]
    if not gated:
        raise pytest.UsageError(
            "GHOSTREPLAY_REQUIRE_PG_TESTS=1 but no @pg_gate tests were selected; "
            "the PostgreSQL release gate would report success with zero coverage."
        )
    collected_ids = {_gate_identity(item.nodeid) for item in gated}
    missing = sorted(REQUIRED_PG_GATE_TESTS - collected_ids)
    if missing:
        raise pytest.UsageError(
            "@pg_gate manifest incomplete under GHOSTREPLAY_REQUIRE_PG_TESTS=1; "
            "these required test identities were not collected: " + ", ".join(missing)
        )
    collected_cases = {item.nodeid for item in gated if "[" in item.nodeid}
    missing_cases = sorted(REQUIRED_PG_GATE_PARAM_CASES - collected_cases)
    if missing_cases:
        raise pytest.UsageError(
            "@pg_gate matrix incomplete under GHOSTREPLAY_REQUIRE_PG_TESTS=1; "
            "these required parametrized cases were not collected: "
            + ", ".join(missing_cases)
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Promote any residual skip on a ``@pg_gate`` test to a failure in required mode.

    The setup gate already turns a missing URL into a failure, but a test body or
    fixture could still ``pytest.skip(...)`` (or carry a ``skip``/``xfail`` marker)
    for some other reason. In required mode that would be an invisible hole in the
    gate, so convert every such skipped report into a failure — **including an
    xfailed one** (a failing ``@pytest.mark.xfail`` / ``pytest.xfail()`` is reported
    as ``outcome="skipped"`` with ``wasxfail``, and an xfailed required proof must
    not exit green). Developer-default mode is untouched, so ``@pg_gate`` tests
    still skip cleanly without a URL.

    This wrapper is registered after core (via conftest ``pytest_plugins``), so it
    is the outermost makereport wrapper and observes the report *after* the core
    skipping plugin has already converted a failing xfail into a skip.
    """
    report = yield
    if _require_pg() and _is_gated(item) and report.skipped:
        was_xfail = hasattr(report, "wasxfail")
        report.outcome = "failed"
        report.longrepr = (
            f"@pg_gate test {item.nodeid} was "
            f"{'XFAILED' if was_xfail else 'SKIPPED'} under "
            f"GHOSTREPLAY_REQUIRE_PG_TESTS=1 (residual "
            f"{'xfail' if was_xfail else 'skip'} promoted to failure): "
            f"{report.longrepr}"
        )
    return report


# ---------------------------------------------------------------------------
# Shared migrated-schema fixtures (moved verbatim from conftest.py).
#
# These exercise behaviour SQLite cannot: real SELECT ... FOR UPDATE row locks
# and the partial unique index on blunder_reviews. The schema under test is the
# ALEMBIC-MIGRATED one (never create_all from models, never drop_all), so PG
# behaviour tests always exercise the real migrated DDL. Session-scoped schema;
# per-test isolation via TRUNCATE.
# ---------------------------------------------------------------------------


def _normalized_pg_url(raw: str) -> str:
    # Imported lazily so plugin import stays cheap and app-independent at collect.
    from app.database_url import _normalize_postgres_scheme

    return _normalize_postgres_scheme(raw)


@pytest.fixture(scope="session")
def pg_engine():
    url = _pg_url()
    if not url:
        if _require_pg():
            pytest.fail(
                "GHOSTREPLAY_REQUIRE_PG_TESTS=1 but GHOSTREPLAY_TEST_PG_URL is not set",
                pytrace=False,
            )
        pytest.skip("GHOSTREPLAY_TEST_PG_URL not set")
    url = _normalized_pg_url(url)

    # Ensure the migrated schema via Alembic (idempotent: a no-op when CI has
    # already run `alembic upgrade head`). env.py resolves the URL from
    # DATABASE_URL, so point it at the test DB for the duration of the upgrade.
    alembic_ini = pathlib.Path(__file__).resolve().parent / "alembic.ini"
    prior_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(alembic_ini)), "head")
    finally:
        if prior_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior_database_url

    # pool_pre_ping mirrors app/db.py. One engine backs the WHOLE gate run, so a
    # pooled connection can sit for minutes between checkouts and can be killed out
    # from under the pool (the capture suite SIGKILLs backends, the migration suite
    # terminates them). Without the ping, the next checkout of such a connection
    # surfaces as an opaque 500 in whatever test happens to draw it —
    # g-rating-serialize-flake, where the victim is never the culprit.
    pg = create_engine(url, pool_pre_ping=True)
    _LIVE_PG_ENGINE["engine"] = pg
    try:
        yield pg
    finally:
        _LIVE_PG_ENGINE["engine"] = None
        pg.dispose()


@pytest.fixture(autouse=True)
def _pg_pool_leak_guard():
    """Fail the test that STRANDS a pooled connection, not the next one to need it.

    ``pg_engine``'s pool is capped at (pool_size + max_overflow) for the entire run,
    so a test that leaves a connection checked out — a request thread still blocked
    at teardown, a ``Session`` never closed — permanently shrinks the pool for every
    later test. The eventual symptom is a checkout timeout raised inside a request,
    answered by the app's generic 500 handler, in a test that did nothing wrong; that
    is precisely the order-flake shape this guard exists to attribute
    (g-rating-serialize-flake).

    Reads the engine the ``pg_engine`` fixture published rather than requesting the
    fixture, so the guard can be autouse over the WHOLE suite without ever building
    an engine: with no PostgreSQL URL there is nothing published and this is a no-op.
    A short poll absorbs a connection still in flight back to the pool; a real leak
    then fails HERE and the pool is replaced so the rest of the run is not poisoned.
    """
    yield
    engine = _LIVE_PG_ENGINE["engine"]
    if engine is None:
        return
    pool = engine.pool
    deadline = time.perf_counter() + _POOL_DRAIN_GRACE_SECONDS
    while pool.checkedout() and time.perf_counter() < deadline:
        time.sleep(0.05)
    stranded = pool.checkedout()
    if stranded:
        # Replace the pool so ONE leaking test does not cascade into every test after
        # it. Connections still checked out are closed when (if) they are returned.
        engine.dispose()
        pytest.fail(
            f"{stranded} PostgreSQL connection(s) still checked out at teardown; this "
            "test strands pool capacity for the rest of the run (close every Session / "
            "Connection it opens, and join every thread it starts)",
            pytrace=False,
        )


def _other_backends(engine) -> str:
    """Every OTHER backend on this database, as a diagnostic block.

    Read on a fresh connection with autocommit so it works even from a failed
    transaction. Anything here in ``active`` or ``idle in transaction`` while a test
    fixture is truncating is a writer the suite does not own — a leaked daemon bound
    to ``app.db.SessionLocal`` (which points at THIS database whenever DATABASE_URL
    does, as it does in CI), a stray subprocess, or a second pytest run.
    """
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            rows = conn.execute(text(
                "SELECT pid, application_name, state, wait_event_type, wait_event, "
                "       xact_start, left(query, 300) AS query "
                "FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "ORDER BY xact_start NULLS LAST"
            )).mappings().all()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never mask the real error
        return f"  <could not read pg_stat_activity: {exc!r}>"
    return "\n".join(f"  {dict(r)}" for r in rows) or "  <no other backends>"


def _truncate_all(engine, table_names: str) -> None:
    """Reset every table for one test, and name the intruder when that is impossible.

    ``lock_timeout`` turns an indefinite block into a fast, attributable failure, and
    both that and a deadlock are re-raised WITH the concurrent-backend dump. Without
    it the suite reports an opaque ``DeadlockDetected`` inside a fixture and the
    culprit — some writer still touching the shared test database — is invisible
    (g-rating-serialize-flake).
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL lock_timeout = '{_TRUNCATE_LOCK_TIMEOUT}'"))
            conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
            # Re-seed the evidence_epoch singleton the TRUNCATE just removed — its
            # triggers UPDATE ... WHERE id = 1 and silently no-op without the row.
            conn.execute(text("INSERT INTO evidence_epoch (id, value) VALUES (1, 0)"))
    except Exception as exc:  # noqa: BLE001 - re-raised, enriched
        raise RuntimeError(
            f"per-test TRUNCATE of the shared PostgreSQL test database failed: {exc}\n"
            "Something outside this test is holding locks on it. Backends at the "
            f"moment of failure:\n{_other_backends(engine)}"
        ) from exc


def _make_isolated_pg_session_factory(pg_engine):
    """Reset the shared schema before constructing one test's Session factory."""
    from app.models import Base

    table_names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    _truncate_all(pg_engine, table_names)
    return sessionmaker(autocommit=False, autoflush=False, bind=pg_engine)


@pytest.fixture
def pg_session_factory(pg_engine):
    """Session factory backed by a clean migrated schema for every test."""
    return _make_isolated_pg_session_factory(pg_engine)


@pytest.fixture
def pg_client(pg_engine, pg_session_factory):
    """TestClient backed by the isolated PostgreSQL Session factory.

    Overrides get_db AFTER the autouse SQLite ``_db_override`` so Postgres wins.
    Each request gets its own session, so concurrent requests can contend for
    real row locks.
    """
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    def _override_pg_db():
        db = pg_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_pg_db
    with patch("app.main.engine", pg_engine), patch(
        "app.main.get_scheduler"
    ), patch("app.main.get_evidence_scheduler"), patch(
        "app.main.get_baseline_scheduler"
    ), patch("app.main.start_prewarm"):
        with TestClient(app) as pg_test_client:
            yield pg_test_client
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Disposable-database fixture for migration tests.
#
# Migration tests need to upgrade a database from base, which the shared
# session-scoped ``pg_engine`` (already at head) cannot provide. ``pg_migration_db``
# creates a throwaway database, yields its URL, and drops it on teardown under a
# strict safety contract so a misconfigured maintenance URL can never touch the
# shared test database or anything else:
#
#   * maintenance authority comes ONLY from GHOSTREPLAY_TEST_PG_MAINT_URL;
#   * every created/dropped name must match ghostreplay_mig_test_<token> and must
#     not equal the shared test database name;
#   * CREATE/DROP run on an autocommit maintenance connection, and teardown first
#     terminates lingering connections to the disposable database;
#   * required mode fails on a missing maintenance URL instead of skipping.
# ---------------------------------------------------------------------------

_DISPOSABLE_DB_RE = re.compile(r"^ghostreplay_mig_test_[0-9a-f]+$")


def _shared_test_db_name() -> str | None:
    """Database name of the shared test URL (guard: never drop this)."""
    raw = _pg_url()
    if not raw:
        return None
    try:
        return make_url(_normalized_pg_url(raw)).database
    except Exception:
        return None


def _assert_disposable(name: str) -> None:
    """Refuse any name that is not a disposable ghostreplay_mig_test_* database.

    Called before BOTH create and drop so a corrupted name can never cause a
    CREATE/DROP against a real database.
    """
    if not _DISPOSABLE_DB_RE.match(name):
        raise RuntimeError(f"refusing to CREATE/DROP non-disposable database name: {name!r}")
    shared = _shared_test_db_name()
    if shared is not None and name == shared:
        raise RuntimeError(f"refusing to CREATE/DROP the shared test database: {name!r}")


def _require_maint_url_or_gate() -> str:
    """Return the normalized maintenance URL, or skip/fail per gate policy.

    Extracted so the required-mode failure path is unit-testable without driving
    a full fixture setup.
    """
    maint_url = _pg_maint_url()
    if not maint_url:
        if _require_pg():
            pytest.fail(
                "GHOSTREPLAY_REQUIRE_PG_TESTS=1 but GHOSTREPLAY_TEST_PG_MAINT_URL is not set",
                pytrace=False,
            )
        pytest.skip("GHOSTREPLAY_TEST_PG_MAINT_URL not set; disposable-DB migration test skipped")
    return _normalized_pg_url(maint_url)


@pytest.fixture
def pg_migration_db():
    maint_url = _require_maint_url_or_gate()
    db_name = f"ghostreplay_mig_test_{uuid.uuid4().hex}"
    _assert_disposable(db_name)  # validate the freshly minted name before touching the server

    # Autocommit: CREATE DATABASE / DROP DATABASE cannot run inside a transaction.
    maint_engine = create_engine(maint_url, isolation_level="AUTOCOMMIT")
    # render_as_string(hide_password=False), NOT str(): str() masks the password
    # as *** and the disposable URL would fail to connect wherever a password is set.
    disposable_url = make_url(maint_url).set(database=db_name).render_as_string(hide_password=False)
    try:
        with maint_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        yield disposable_url
    finally:
        _assert_disposable(db_name)  # re-validate before the drop, defensively
        with maint_engine.connect() as conn:
            # Terminate lingering sessions on the disposable DB so DROP succeeds.
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ).bindparams(d=db_name)
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        maint_engine.dispose()
