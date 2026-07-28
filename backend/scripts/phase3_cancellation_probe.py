"""Phase 3 qualification controller — drives the SHIPPED revision and, in the
cancel modes, breaks it from a second session.

Sizing qualification (`docs/release_b_runbook.md` §10) asks for runs of
`20260719_01` with every guard armed, on a fresh restore, driven through
`alembic upgrade` so `env.py`, the migration guard and the stall probe are all in
the path. This is the controller that takes them. It is a MEASUREMENT tool in the
same category as `size_accuracy_backfill.py`: run by hand against a disposable
copy, never on a deployment path, and it modifies no revision SQL and relaxes no
guard.

Four modes, and the mode names what is being qualified:

  none    Run to completion and record the terminal state. The timing evidence
          (§10 3a / 3a'). No trigger is created and nothing is cancelled.
  batch   Cancel the BACKFILL phase's guarded UPDATE, in per-batch mode, on the
          runner's own connection.
  repair  Cancel the REPAIR phase's per-batch transaction, in per-batch mode,
          once it holds the WHOLE batch dirty.
  atomic  Cancel the soundness assertion in atomic mode, where one transaction
          holds the whole population dirty on the migration connection.

WHY `repair` EXISTS, AND WHY `batch` IS NOT ENOUGH. `TEARDOWN_ALLOWANCE_MS` is
scoped to a batch of EITHER phase, and the larger of the two is not the
backfill's: `REPAIR_BATCH_SIZE` divides by a cheaper per-row cost, so it exceeds
`MAX_BATCH_SIZE` (1,000 against 646). The revision's own docstring requires the
breach path to be measured on a transaction of at least
`max(MAX_BATCH_SIZE, REPAIR_BATCH_SIZE)` rows. A cancel in the backfill phase
therefore breaks the SMALLER of the two admitted transactions, and a qualification
built only from it has not exercised the shape the constant is sized for.

Four gates before any cancel. The first two are NECESSARY BUT NOT SUFFICIENT and
the last two are what make the evidence positive:

  (a) the target holds a granted ExclusiveLock on its own transactionid.
      NOT sufficient: `SELECT ... FOR NO KEY UPDATE` assigns an XID, and in batch
      mode that is the FIRST statement of every batch — so this is true from
      selection onward, before a single accuracy row is updated.
  (b) a row it should be holding raises 55P03 under NOWAIT. Fails the same way and
      for the same reason: selection is what took that lock.
  (c) STATEMENT IDENTITY — pg_stat_activity shows the target active and running
      the statement we mean to cancel, matched on its MARKER COMMENT.
  (d) DIRTY-TRANSACTION EVIDENCE — the park trigger's advisory lock is visible.
      An `AFTER ... FOR EACH STATEMENT` trigger fires only once the statement has
      updated EVERY row it will update, so while it parks, the transaction holds a
      COMPLETE dirty batch. In `repair` mode the lock's OBJID carries the count of
      `repair_update` statements this transaction has issued, so gate (d) proves
      not merely "dirty" but "dirty at the full batch size" — which is the whole
      reason that mode exists.

Gate (d) is checked BEFORE gate (b), in every mode. Gate (b) acquires a real row
lock when it succeeds, and probing it during the write phase races the runner:
under `SKIP LOCKED` selection the prober's lock silently shrinks the batch, and
under the repair phase's blocking `FOR NO KEY UPDATE` it stalls the runner against
its own armed `lock_timeout`. Waiting for the park puts every probe strictly after
the writes.

The trigger exists on the disposable copy only, is dropped afterwards, alters no
revision SQL and is on no deployment path — the same category as
`pg_cancel_backend()` from a second session.

The headline number is `cancel_to_unlock_ms`, measured HERE: from cancel issuance
to the moment a competing `FOR NO KEY UPDATE NOWAIT` on a held row ACQUIRES. Not
the runner's `teardown_ms`, which starts when Python issues ROLLBACK — after the
cancel was delivered, after PostgreSQL reached the next interrupt point, after the
statement unwound. Every one of those is time a writer spends blocked.

A trial is DISCARDED, not recorded, when it measured the wrong thing — and that is
ENFORCED here rather than left to whoever reads the output (:func:`validate`).
Gates that never held, a `pg_cancel_backend` that returned false, a held row that
never unlocked, a `none` run that failed, or a leaked trigger or advisory lock: none
of those write `--out`, and all of them exit nonzero. The rejected run is written
beside the target as `<out>.rejected` so it can still be diagnosed.

The cancel modes are judged on their OUTCOME as well as their cancel, because a
successful cancel and a broken run are not the same claim. `pg_cancel_backend`
returns true against a backend that was already finishing: the row then unlocks
because the transaction COMMITTED, the migration stamps `alembic_version` and
validates the CHECK, and the artifact carries a real-looking `cancel_to_unlock_ms`
measured off a teardown that never happened. So a cancel trial must also show a
nonzero exit, the stamp still at the revision's `down_revision`, the CHECK still
`NOT VALID`, and the per-mode populations of a rolled-back transaction.

WHAT THE FIXTURE IS gets recorded the same way, because counts do not identify one.
The sizing harness documents that taking repair candidates by `ORDER BY id` instead
of `ORDER BY md5(id)` yields the same K candidates and the same K deleted plies while
selecting the K lowest ids — identical populations and identical relation dimensions,
every scan-bearing statement measured at a fraction of its real cost. So each run
records `fixture_identity` (content digests of the accuracy-bearing columns: WHICH
rows, not how many), `fixture_provenance` (what `phase3_prepare.py` stamped on the
copy before anything seeded it), and `frozen_symbols` (the synthesis functions
themselves, bound per symbol rather than per file).

The one that most needs enforcing is `statement_timeout`. A park that outlives its
batch's own budget raises SQLSTATE 57014 — the *same* code a cancel raises — so the
run looks cancelled, reports a plausible unlock time, and is measuring the timeout
path. Only the message text separates them, so `cancel_cause` is read out of the log
and anything but `user_request` is discarded. The fix is a shorter park.

A breach of `TEARDOWN_ALLOWANCE_MS` is NOT a discard. That is a real finding and is
recorded loudly; only trials that measured the wrong thing are thrown away.

This script runs a migration against its target and, in the cancel modes, installs a
trigger and cancels backends — so it carries the same fence as
`size_accuracy_backfill.py`: `--confirm-mutates`, plus a disposable-name check.

    python scripts/phase3_cancellation_probe.py DB none   --confirm-mutates --out a.json
    python scripts/phase3_cancellation_probe.py DB repair --confirm-mutates --park 0.4 --out r.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from scripts.phase3_fixture_guard import (  # noqa: E402
    confirm_mutates,
    content_digest,
    fixture_digest,
    read_provenance,
    semantic_fingerprint,
    symbol_fingerprint,
)

REVISION = "20260719_01"

#: Which backend holds the row locks differs BY MODE, and a single hard-coded
#: application_name gets it wrong. In per-batch mode the runner has its own
#: connection; in atomic mode THERE IS NO RUNNER CONNECTION — the revision runs on
#: Alembic's migration connection. A probe filtering on the runner's name during
#: an atomic run matches nothing, cancels nothing, and reports success.
TARGET_APP = {
    "batch": "ghostreplay_accuracy_backfill",
    "repair": "ghostreplay_accuracy_backfill",
    "atomic": "ghostreplay_alembic_migration",
}
#: The statement to cancel, by marker. In atomic mode it is the SOUNDNESS
#: ASSERTION: the revision runs validate -> backfill -> repair -> assertions in
#: order, so a backend active inside it has by construction already issued every
#: mutation of the run and holds the whole population dirty.
TARGET_MARKER = {
    "batch": "guarded_update",
    "repair": "repair_update",
    "atomic": "soundness_assert",
}
#: The runner mode each probe mode drives the revision in.
RUN_MODE = {"batch": "batch", "repair": "batch", "atomic": "atomic", "none": "atomic"}

PROBE_CLASSID = 424242
#: Objid for the modes whose park is a single statement, so the lock's identity
#: carries no count. `repair` overrides this with the count itself.
PARK_OBJID_SINGLE = 990001

#: Everything whose BEHAVIOUR the runs are evidence about. Recorded per file
#: because a qualification run is taken at a working-tree state, and a commit SHA
#: only becomes the durable handle once that state lands.
#:
#: THIS FILE IS IN THE LIST. The controller decides when the gates hold and what
#: gets recorded, so a change to it is as capable of invalidating a run as a change
#: to the revision — a probe that cancels at the wrong moment produces a number
#: that is wrong in exactly the way nothing downstream can detect. Self-inclusion
#: also means the runs have to be taken LAST, after the controller is final.
#:
#: THE REVISION'S OWN IMPORTS ARE IN IT TOO. `20260719_01` deliberately does not
#: import `app.accuracy`; it pins the frozen v1 algorithm, and every per-row cost
#: these runs measured is that algorithm executing. Listing the revision but not
#: what it imports would let an edit to the scoring path change what the migration
#: does — and how long a batch holds its rows — with the gate still green. The rule
#: for what belongs here is "behaviour the runs could not observe": the fixture is
#: covered instead by `populations_before` and `dimensions_before`, which record
#: what the seeding actually produced rather than trusting the script that made it.
#:
#: And the guard, because it computes the fingerprints. A verification protocol
#: that does not cover its own comparator can be weakened by editing the
#: comparator, which is the one change nothing else in the set would show.
#:
#: And the PREPARER, which is why it is Python and not the shell script it started
#: as: it picks the template, decides that stamping happens after the clone and
#: before any seeding, and reconstructs the pre-revision state. All three decide
#: what these runs are evidence about, and `ast.parse` has nothing to say about a
#: `.sh` file — a component that cannot be fingerprinted cannot be part of the set
#: that makes the artifacts expire.
FROZEN_FILES = (
    f"alembic/versions/{REVISION}_backfill_session_player_accuracy.py",
    "app/accuracy_v1.py",
    "app/accuracy_rows_v1.py",
    "app/migration_guard.py",
    "alembic/env.py",
    "scripts/phase3_cancellation_probe.py",
    "scripts/phase3_fixture_guard.py",
    "scripts/phase3_seed_populations.py",
    "scripts/phase3_prepare.py",
)

#: THE SYNTHESIS, bound per symbol rather than per file. `size_accuracy_backfill.py`
#: is ~2,500 lines of Phase 1 and Phase 2 machinery and only these decide what a
#: fixture IS; whole-file fingerprinting would expire every Phase 3 run whenever
#: `derive` changed, and a gate that fires on unrelated edits gets overridden.
#:
#: This is bound because counts do not identify a fixture. `synthesize_repair`'s own
#: docstring records the case: taking candidates by `ORDER BY id` instead of
#: `ORDER BY md5(id)` still produces exactly K candidates and deletes exactly K
#: plies — identical populations, identical relation dimensions — while selecting
#: the K LOWEST ids, which lets a merge join terminate a few percent into
#: `session_moves` and measures every scan-bearing statement at a fraction of its
#: real cost. `analyze_after_synthesis` is here for the same reason from the other
#: direction: stale statistics turn `REPAIR_POPULATE_SQL` from 155 ms into 7 minutes
#: by picking a nested loop, on a population that is otherwise identical.
FROZEN_SYMBOLS = {
    "scripts/size_accuracy_backfill.py": (
        "MIN_SYNTHESIZED_REPAIR",
        "analyze_after_synthesis",
        "check_repair_sample_size",
        "synthesize_repair",
        "synthesize_stale",
    ),
}


def revision_module():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg).get_revision(REVISION).module


def park_trigger_sql(mode: str, park_s: float, target_n: int) -> str:
    """The park. Two shapes, because the two batch phases are shaped differently.

    The backfill applies ONE `UPDATE ... WHERE id = ANY(...)` per batch, so an
    AFTER-STATEMENT trigger fires exactly once with the whole batch already
    written — the lock's existence is the evidence.

    The repair phase applies one single-row `UPDATE` per candidate inside ONE
    transaction, so the same trigger would fire `REPAIR_BATCH_SIZE` times, each
    with only one more row dirty. Parking on the first is evidence about a
    ONE-ROW transaction. So the repair shape COUNTS instead: a transaction-local
    setting (`is_local => true`, so it dies with the transaction rather than
    leaking into the next batch) is incremented per `repair_update`, and the park
    happens only on the Nth — at which point the transaction holds N rows locked
    and dirty. The count is published as the advisory OBJID so the evidence is
    read from outside the parked process rather than inferred.
    """
    if mode == "repair":
        guard = f"""
  IF current_query() NOT LIKE '%ghostreplay:{TARGET_MARKER[mode]}%' THEN
    RETURN NULL;
  END IF;
  n := coalesce(nullif(current_setting('ghostreplay_probe.updates', true), '')::int, 0) + 1;
  PERFORM set_config('ghostreplay_probe.updates', n::text, true);
  IF n < {target_n} THEN
    RETURN NULL;
  END IF;"""
        objid = "n"
    else:
        guard = ""
        objid = str(PARK_OBJID_SINGLE)
    return f"""
CREATE OR REPLACE FUNCTION _ghostreplay_probe_park() RETURNS trigger AS $$
DECLARE n int;
BEGIN{guard}
  PERFORM pg_advisory_xact_lock({PROBE_CLASSID}, {objid});
  PERFORM pg_sleep({park_s});
  RETURN NULL;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS _ghostreplay_probe_park_au ON game_sessions;
CREATE TRIGGER _ghostreplay_probe_park_au AFTER UPDATE ON game_sessions
  FOR EACH STATEMENT EXECUTE FUNCTION _ghostreplay_probe_park();
"""


DROP_TRIGGER = """
DROP TRIGGER IF EXISTS _ghostreplay_probe_park_au ON game_sessions;
DROP FUNCTION IF EXISTS _ghostreplay_probe_park();
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("database")
    ap.add_argument("mode", choices=["none", "batch", "repair", "atomic"])
    ap.add_argument("--park", type=float, default=0.4, help="park seconds")
    ap.add_argument("--url", default=None, help="full SQLAlchemy URL; overrides --database")
    ap.add_argument("--out", default=None, help="write the artifact here")
    ap.add_argument("--timeout", type=float, default=180.0, help="gate-wait deadline")
    ap.add_argument("--confirm-mutates", action="store_true")
    args = ap.parse_args()

    mode = args.mode
    url = args.url or os.environ.get("GHOSTREPLAY_PHASE3_URL_TEMPLATE", "").format(
        database=args.database
    ) or f"postgresql+psycopg://localhost:5432/{args.database}"

    mod = revision_module()
    engine = create_engine(url, poolclass=__import__("sqlalchemy.pool", fromlist=["x"]).NullPool)

    # The fence, before anything opens a subprocess or installs a trigger. Same
    # shape as `size_accuracy_backfill.py`'s, because this script is strictly more
    # destructive than that one: it runs a migration to completion, cancels
    # backends, and leaves the copy mid-revision.
    with engine.connect() as c:
        # RECORD WHAT THE CONNECTION RESOLVED TO, not what was typed. `--url` is a
        # full override, so the positional argument and the database actually
        # measured can differ while both are disposable and both pass the name
        # check — and the artifact would then identify a database it never touched.
        # `expect=` also refuses the divergence, so the two can never disagree.
        database = confirm_mutates(
            c,
            confirmed=args.confirm_mutates,
            expect=args.database,
            what=(
                "this script RUNS THE MIGRATION against the target, and in the cancel "
                "modes installs a parking trigger and cancels backends"
            ),
        )

    out: dict = {
        "kind": "phase3_run",
        "mode": mode,
        "runner_mode": RUN_MODE[mode],
        "revision": REVISION,
        # The revision's own `down_revision`, so the terminal-state check below is
        # against what the migration says it descends from rather than a literal
        # repeated here that would survive the graph moving.
        "down_revision": mod.down_revision,
        "database": database,
        # Two records of the same files, and only one of them is a gate.
        # `sha256` answers "which bytes produced these numbers" and moves when a
        # docstring is reflowed. `fingerprint` is the parsed AST with docstrings
        # stripped, so it is blind to prose and layout and moves on any change to a
        # statement, a literal or an expression — which is what "a later edit to
        # the runner invalidates these runs" actually means. The test compares the
        # fingerprints; enforcing the digests would demand a re-run for edits that
        # cannot affect a measurement, and an enforcement everyone overrides is
        # worse than none.
        "frozen_files": {f: content_digest(BACKEND / f) for f in FROZEN_FILES},
        "frozen_fingerprints": {f: semantic_fingerprint(BACKEND / f) for f in FROZEN_FILES},
        # Per-symbol, for the one file where whole-file coverage would be too broad
        # to survive. Flattened to `path::symbol` so the artifact stays a flat map
        # and a test can compare it against the live tree in one expression.
        "frozen_symbols": {
            f"{rel}::{name}": digest
            for rel, names in FROZEN_SYMBOLS.items()
            for name, digest in symbol_fingerprint(BACKEND / rel, names).items()
        },
        "constants": {
            "MAX_BATCH_SIZE": mod.MAX_BATCH_SIZE,
            "REPAIR_BATCH_SIZE": mod.REPAIR_BATCH_SIZE,
            "TEARDOWN_ALLOWANCE_MS": mod.TEARDOWN_ALLOWANCE_MS,
            "MAX_WRITER_STALL_MS": mod.MAX_WRITER_STALL_MS,
        },
    }

    # Populations BEFORE the run, read through the revision's own scans — the
    # batch sizes the run will use are functions of these, and the artifact has to
    # carry them or `dirty_rows_at_cancel` is an unsupported claim.
    with engine.connect() as c:
        n_stale = mod.remaining_scan(c, mod.BACKFILL_REMAINING_SQL)[0]
        n_repair, repair_ids = mod.remaining_scan(c, mod.REPAIR_POPULATION_COUNT_SQL)
        out["server_version"] = c.execute(text("SELECT version()")).scalar()
        out["dimensions_before"] = {
            k: int(v)
            for k, v in c.execute(text(mod.DIMENSION_PROBE_SQL)).mappings().one().items()
        }
        # WHICH ROWS, not how many. Counts and dimensions are both blind to the
        # selection: the harness documents that taking repair candidates by
        # `ORDER BY id` rather than `ORDER BY md5(id)` produces the same 1,000
        # candidates and the same 1,000 deleted plies while measuring every
        # scan-bearing statement at a fraction of its cost. Digesting the
        # accuracy-bearing columns makes the fixture an observation in the artifact
        # instead of a property of whatever the seeding script did that day — and
        # lets independently prepared copies be shown to be the same fixture.
        out["fixture_identity"] = fixture_digest(c)
        # And what this copy was CLONED from, stamped by `phase3_prepare.py` before
        # anything seeded it. No post-seed read can recover it: synthesis deletes
        # plies and rewrites accuracy columns.
        out["fixture_provenance"] = read_provenance(c)
    out["populations_before"] = {"n_stale": n_stale, "n_repair": n_repair}

    # What the cancelled transaction will hold, and WHERE THAT NUMBER COMES FROM.
    # Recorded as a claim plus its evidence, because the three modes prove it
    # three different ways and a bare integer would hide that.
    if mode == "repair":
        target_n = min(mod.REPAIR_BATCH_SIZE, n_repair)
        dirty = {
            "value": target_n,
            "evidence": (
                "advisory objid == the count of ghostreplay:repair_update statements "
                "issued by the cancelled transaction, published by the park trigger "
                "and read from a second session"
            ),
        }
    elif mode == "batch":
        target_n = min(mod.MAX_BATCH_SIZE, n_stale)
        dirty = {
            "value": target_n,
            "evidence": (
                "one ghostreplay:guarded_update over min(MAX_BATCH_SIZE, n_stale); the "
                "AFTER-STATEMENT park proves the statement had written every row"
            ),
        }
    else:
        target_n = n_stale + n_repair
        dirty = {
            "value": target_n,
            "evidence": (
                "atomic mode is one transaction; both phases completed before the "
                "soundness assertion, with an earlier park observed as gate (d)"
            ),
        }
    out["dirty_rows_at_cancel"] = dirty if mode != "none" else None

    if mode == "none":
        return finish(engine, args, out, run(url, RUN_MODE[mode]), mod)

    if target_n < 1:
        out["result"] = "refused: nothing seeded to lock"
        return finish(engine, args, out, None, mod)

    park_objid = target_n if mode == "repair" else PARK_OBJID_SINGLE
    out["park_seconds"] = args.park
    out["park_objid"] = park_objid

    with engine.begin() as c:
        c.execute(text(park_trigger_sql(mode, args.park, target_n)))
        # The victim must be a row the target will actually hold. In `repair` mode
        # that is a repair CANDIDATE — an arbitrary ended-visible row need not be
        # one, and gate (b) against a row nobody locked is a gate that never opens.
        #
        # Sampled from the convergence scan, which is sound while the population
        # fits in ONE batch (n_repair == REPAIR_BATCH_SIZE here, so the first page
        # is the whole set). Past that, the sample may name a row a later page
        # would have taken and the victim has to be drawn from the first page
        # instead. That failure is loud — the gates never all hold and the run
        # reports "nothing cancelled" — but it is a real edge for a larger fixture.
        if mode == "repair":
            victim = repair_ids[0]
        else:
            victim = str(
                c.execute(
                    text(
                        "SELECT id FROM game_sessions WHERE status='ended' "
                        "AND (session_mode='normal' OR drill_state='converted') "
                        "ORDER BY id LIMIT 1"
                    )
                ).scalar()
            )
    out["victim"] = victim

    # The prober, the canceller and the stat reader are three SEPARATE long-lived
    # connections, and none of the three is a convenience.
    #
    # A canceller drawn from a pool per call can be handed the very backend it is
    # about to cancel, and a silent self-cancel looks exactly like a fast unlock —
    # biasing the number DOWN.
    #
    # `stat` is separate and AUTOCOMMIT because pg_stat_activity is SNAPSHOTTED PER
    # TRANSACTION: pgstat_read_current_status() builds a backend-local copy on
    # first access and holds it until the transaction ends. A prober that reads it
    # inside a long-lived transaction therefore sees the process table AS IT WAS
    # when that transaction began — so a probe that starts before the runner
    # connects never sees the runner AT ALL, polls a stale snapshot for the whole
    # run, and reports "gates never held" while the statement it meant to cancel
    # came and went. Observed here, and it is instrumentation that quietly does
    # nothing while looking like a clean result.
    prober = engine.connect()
    canceller = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    stat = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    own_pid = canceller.execute(text("SELECT pg_backend_pid()")).scalar()
    prober_pid = prober.execute(text("SELECT pg_backend_pid()")).scalar()
    prober.rollback()

    proc = run(url, RUN_MODE[mode])

    def gate_a(pid: int) -> bool:
        return bool(
            stat.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE pid = :p AND locktype='transactionid' "
                    "AND mode='ExclusiveLock' AND granted"
                ),
                {"p": pid},
            ).scalar()
        )

    def gate_b() -> bool:
        """A row the target should be holding is unlockable -> 55P03."""
        try:
            prober.execute(
                text(
                    "SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) "
                    "FOR NO KEY UPDATE NOWAIT"
                ),
                {"i": victim},
            )
            prober.rollback()
            return False
        except DBAPIError as e:
            prober.rollback()
            return "55P03" in str(getattr(e.orig, "sqlstate", "")) or "could not obtain lock" in str(e)

    def gate_c(pid: int) -> bool:
        return bool(
            stat.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid = :p AND state='active' "
                    "AND query LIKE '%ghostreplay:' || :m || '%'"
                ),
                {"p": pid, "m": TARGET_MARKER[mode]},
            ).scalar()
        )

    def gate_d(pid: int) -> bool:
        return bool(
            stat.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE pid = :p AND locktype='advisory' "
                    "AND classid = :c AND objid = :o AND granted"
                ),
                {"p": pid, "c": PROBE_CLASSID, "o": park_objid},
            ).scalar()
        )

    def target_pid() -> int | None:
        return stat.execute(
            text(
                "SELECT pid FROM pg_stat_activity WHERE application_name = :a "
                "AND state='active' AND pid NOT IN (:own, :prb) LIMIT 1"
            ),
            {"a": TARGET_APP[mode], "own": own_pid, "prb": prober_pid},
        ).scalar()

    # In atomic mode the probe must have SEEN an earlier UPDATE park before it arms
    # on the assertion, so "we are past the mutations" is evidenced, not assumed.
    saw_update_park = mode != "atomic"
    trace: list = []
    t0 = time.monotonic()
    deadline = t0 + args.timeout
    gates: dict = {}
    pid = None

    while time.monotonic() < deadline and proc.poll() is None:
        pid = target_pid()
        if pid is None:
            time.sleep(0.002)
            continue
        prober.rollback()
        a, c_, d = gate_a(pid), gate_c(pid), gate_d(pid)
        trace.append((round(time.monotonic() - t0, 3), pid, a, c_, d))
        if not saw_update_park:
            if d:
                saw_update_park = True
            time.sleep(0.002)
            continue
        if not (a and c_ and d):
            time.sleep(0.002)
            continue
        # Only now: gate (b) takes a real lock when it succeeds, so it must not run
        # until the writes are provably done.
        b = gate_b()
        if not b:
            time.sleep(0.002)
            continue
        gates = {
            "a_transactionid_xlock": a,
            "b_55P03_on_held_row": b,
            "c_statement_identity": c_,
            "d_dirty_batch_advisory": d,
        }
        break

    out["target_pid"] = pid if gates else None
    out["canceller_pid"] = own_pid
    out["gates"] = gates
    out["saw_update_park_first"] = saw_update_park
    out["trace_len"] = len(trace)
    out["trace_sample"] = trace[:: max(1, len(trace) // 20)][:20]

    if not gates:
        out["result"] = "gates never all held; nothing cancelled"
    else:
        assert pid != own_pid, "self-cancel: discard"
        t_cancel = time.monotonic()
        cancelled = canceller.execute(text("SELECT pg_cancel_backend(:p)"), {"p": pid}).scalar()
        # Poll a row the target holds until the lock comes off. The first execution
        # that ACQUIRES rather than raising 55P03 marks the release.
        t_released = None
        while time.monotonic() - t_cancel < 60:
            try:
                prober.execute(
                    text(
                        "SELECT id FROM game_sessions WHERE id = CAST(:i AS uuid) "
                        "FOR NO KEY UPDATE NOWAIT"
                    ),
                    {"i": victim},
                )
                t_released = time.monotonic()
                prober.rollback()
                break
            except DBAPIError:
                prober.rollback()
        out["pg_cancel_backend"] = cancelled
        out["cancel_to_unlock_ms"] = None if t_released is None else (t_released - t_cancel) * 1000.0
        out["result"] = "cancelled"

    for conn in (prober, canceller, stat):
        conn.close()
    with engine.begin() as c:
        c.execute(text(DROP_TRIGGER))
    return finish(engine, args, out, proc, mod)


def run(url: str, runner_mode: str):
    """Start `alembic upgrade` as a subprocess. NEVER waited on here: the caller
    reads the pipe through `communicate()` in :func:`finish`, and a `wait()` on a
    PIPE'd child deadlocks the moment the log outgrows the buffer."""
    env = dict(os.environ, DATABASE_URL=url, GHOSTREPLAY_ACCURACY_BACKFILL_MODE=runner_mode)
    return subprocess.Popen(
        [sys.executable, "-m", "alembic", "upgrade", REVISION],
        cwd=str(BACKEND),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


#: Log lines the artifact keeps. `canceling statement` is the ONLY thing that
#: separates a cancel from a statement_timeout — both raise 57014 — so it is not
#: optional decoration.
_LOG_KEEP = (
    REVISION,
    "57014",
    "canceling statement",
    "observed_atomic_stall_ms",
    "row-lock hold",
)


def finish(engine, args, out: dict, proc, mod) -> int:
    if proc is not None:
        log = proc.communicate(timeout=900)[0] or ""
        out["alembic_returncode"] = proc.returncode
        kept = [ln for ln in dict.fromkeys(log.splitlines()) if any(k in ln for k in _LOG_KEEP)]
        # Sanitize: absolute paths on the machine that took the run are noise in a
        # committed artifact and are the only host detail the log carries.
        out["log"] = [ln.replace(str(REPO) + "/", "") for ln in kept][-40:]
        low = log.lower()
        out["cancel_cause"] = (
            "user_request"
            if "due to user request" in low
            else "statement_timeout"
            if "due to statement timeout" in low
            else "lock_timeout"
            if "due to lock timeout" in low
            else None
        )

    # TERMINAL STATE, read after the run. This is what says a cancelled run left
    # nothing stamped, and — in the batch modes — what says the per-batch
    # transactions that COMMITTED before the cancel are still committed.
    with engine.connect() as c:
        out["terminal"] = {
            "alembic_version": c.execute(text("SELECT version_num FROM alembic_version")).scalar(),
            "check_convalidated": c.execute(
                text(
                    "SELECT convalidated FROM pg_constraint "
                    "WHERE conname='ck_game_sessions_player_accuracy'"
                )
            ).scalar(),
            "n_stale": mod.remaining_scan(c, mod.BACKFILL_REMAINING_SQL)[0],
            "n_repair": mod.remaining_scan(c, mod.REPAIR_POPULATION_COUNT_SQL)[0],
            "probe_trigger_left": c.execute(
                text("SELECT count(*) FROM pg_trigger WHERE tgname='_ghostreplay_probe_park_au'")
            ).scalar(),
            "advisory_locks_left": c.execute(
                text("SELECT count(*) FROM pg_locks WHERE locktype='advisory'")
            ).scalar(),
        }
    engine.dispose()

    problems = validate(out)
    out["valid"] = not problems
    out["invalid_reasons"] = problems

    blob = json.dumps(out, indent=1, default=str, sort_keys=False)
    print(blob)

    if args.out:
        target = Path(args.out)
        if problems:
            # DISCARDED, NOT RECORDED. Writing an invalid trial to the artifact
            # path is the exact silent-success this probe exists to avoid: the
            # file lands, the caller's `&&` chain continues, and a run that
            # measured nothing sits in the evidence set looking like one that did.
            # It is written BESIDE the target instead, because a rejected trial is
            # the most useful thing there is for working out why — and `.rejected`
            # is not a name anything downstream reads.
            rejected = target.with_suffix(target.suffix + ".rejected")
            _atomic_write(rejected, blob)
            print(
                f"\nDISCARDED — not written to {target}:\n  "
                + "\n  ".join(problems)
                + f"\ndiagnostics: {rejected}",
                file=sys.stderr,
            )
        else:
            _atomic_write(target, blob)

    return 1 if problems else 0


def _atomic_write(path: Path, blob: str) -> None:
    """Write via a sibling temp file and `os.replace`, so a reader never sees half.

    `os.replace` is atomic within a filesystem, and the temp file is a sibling so
    it is on the same one. Without this, a crash mid-write leaves a truncated JSON
    at a path the test suite parses.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(blob + "\n")
    os.replace(tmp, path)


def validate(out: dict) -> list[str]:
    """Why this trial is not evidence. Empty list means it is.

    The contract in the module docstring — "a trial is DISCARDED, not recorded" —
    was previously enforced by the operator reading the output. It is enforced here
    now, because every one of these failures produces an artifact that LOOKS fine:
    gates that never held leave `result` set and every other field populated, a
    `pg_cancel_backend` that returned false leaves a plausible unlock time measured
    off a lock nobody was holding, and a `statement_timeout` raises the same
    SQLSTATE a cancel does.

    What is deliberately NOT here: `cancel_to_unlock_ms < TEARDOWN_ALLOWANCE_MS`.
    A breach is a real finding and has to be recorded, loudly. This function only
    rejects trials that measured the wrong thing.
    """
    problems: list[str] = []
    mode = out["mode"]
    terminal = out.get("terminal") or {}

    if mode == "none":
        if out.get("alembic_returncode") != 0:
            problems.append(f"migration exited {out.get('alembic_returncode')}, expected 0")
        if terminal.get("alembic_version") != REVISION:
            problems.append(
                f"terminal alembic_version {terminal.get('alembic_version')!r}, expected {REVISION!r}"
            )
    else:
        if not out.get("gates"):
            problems.append(f"gates never all held: {out.get('result')!r}")
        if out.get("pg_cancel_backend") is not True:
            problems.append(f"pg_cancel_backend returned {out.get('pg_cancel_backend')!r}")
        if out.get("cancel_to_unlock_ms") is None:
            problems.append("the held row never unlocked; no cancel_to_unlock_ms")
        if out.get("cancel_cause") != "user_request":
            problems.append(
                f"cancel_cause is {out.get('cancel_cause')!r}, not 'user_request' — this "
                "trial measured a timeout, not the cancel path"
            )
        problems += _outcome_problems(out, terminal)

    # Every mode: a run whose copy carries no provenance stamp cannot say what base
    # data it measured, and "which rows" is not recoverable afterwards — synthesis
    # deletes plies and rewrites accuracy columns, so the copy no longer resembles
    # what it was cloned from. An unstamped copy is one `phase3_prepare.py` did not
    # make, and the run on it is a measurement of something unidentified.
    if not out.get("fixture_provenance"):
        problems.append(
            "no fixture provenance on the copy: it was not prepared by "
            "phase3_prepare.py, so this run cannot name the base data it measured"
        )
    if not out.get("fixture_identity"):
        problems.append("no fixture_identity recorded; the fixture is unobserved")

    # Applies to every mode: a leaked trigger or advisory lock silently changes
    # whatever runs on this copy next, so the copy is spent either way.
    if terminal.get("probe_trigger_left"):
        problems.append("the park trigger was left installed on the copy")
    if terminal.get("advisory_locks_left"):
        problems.append(f"{terminal['advisory_locks_left']} advisory lock(s) still held")
    return problems


def _outcome_problems(out: dict, terminal: dict) -> list[str]:
    """Did the cancel actually break the run, and did the broken run roll back?

    Everything above this is about the CANCEL — that it was issued, that it landed,
    that a row unlocked, that 57014 came from a user request. None of it says the
    migration died: `pg_cancel_backend` succeeds against a backend that is already
    finishing, the row unlocks because the transaction committed, and the run goes
    on to stamp `alembic_version` and validate the CHECK. That trial has a real
    `cancel_to_unlock_ms` in it, measured off a teardown that was not a teardown,
    and every field the reader would use to notice looks correct. So the OUTCOME is
    checked too: a nonzero exit, the version still at the revision's own
    `down_revision`, the CHECK still `NOT VALID`, and — per mode, because the three
    differ — the populations.

    The populations are what carry "the cancelled transaction rolled back". The
    version stamp only says the run did not finish; a durable partial mutation
    leaves it untouched.
    """
    problems: list[str] = []
    mode, expected_version = out["mode"], out.get("down_revision")

    if out.get("alembic_returncode") in (None, 0):
        problems.append(
            f"migration exited {out.get('alembic_returncode')!r}: a cancelled run must "
            "fail, and one that succeeded was not the run this cancel broke"
        )
    if not expected_version:
        problems.append("no down_revision recorded; nothing to check the version stamp against")
    elif terminal.get("alembic_version") != expected_version:
        problems.append(
            f"terminal alembic_version {terminal.get('alembic_version')!r}, expected "
            f"{expected_version!r} — a cancelled run leaves the stamp where it found it"
        )
    if terminal.get("check_convalidated") is not False:
        problems.append(
            f"check_convalidated is {terminal.get('check_convalidated')!r}, expected False — "
            "the CHECK stayed NOT VALID unless the run got past validation"
        )

    before = out.get("populations_before")
    if not before:
        problems.append("no populations_before recorded; the terminal counts prove nothing")
        return problems
    got = (terminal.get("n_stale"), terminal.get("n_repair"))
    want = (before.get("n_stale"), before.get("n_repair"))
    if mode == "atomic":
        # ONE transaction over both phases, so nothing at all survives it.
        if got != want:
            problems.append(f"atomic cancel left populations {got}, expected {want} unchanged")
    elif mode == "batch":
        # The park fires on the FIRST `guarded_update` statement, so the cancel
        # breaks the first batch and none has committed.
        if got != want:
            problems.append(f"backfill cancel left populations {got}, expected {want} unchanged")
    elif mode == "repair":
        # Reaching the repair phase REQUIRES the backfill phase to have converged
        # and committed, so a repair trial that still has stale rows never got to
        # the transaction it meant to cancel. The repair batch itself rolled back
        # whole, so its own population is untouched.
        if got[0] != 0:
            problems.append(
                f"repair cancel ran with {got[0]} stale rows left: the backfill phase had "
                "not converged, so this did not cancel a repair-phase transaction"
            )
        if got[1] != want[1]:
            problems.append(
                f"repair cancel left n_repair {got[1]}, expected {want[1]} — the cancelled "
                "batch did not roll back whole"
            )
    return problems


if __name__ == "__main__":
    raise SystemExit(main())
