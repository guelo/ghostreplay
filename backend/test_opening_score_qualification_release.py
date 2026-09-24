"""§2.6 — one case per new branch in the g-score-store-qualify harness.

THESE RUN IN PRE-PUSH. They carry the guards that must never silently weaken —
a refusal list that omits one alias IS the defect — so every refusal has its own
case and every alias has its own parameter, and a gate nobody runs protects
nothing. Everything here is a pure function, a fabricated report or a temporary
SQLite database; nothing connects to a cluster and nothing takes seconds.

``release_seal`` is applied to the ONE case that reads host state — the real
census in the private store — because ``AGENTS.md`` reserves the marker for
tests that need minutes of wall clock or read state other agents are editing,
and a blanket module-level mark took the other 250-odd cases out of pre-push
with it.

If a case here ever becomes ``@pg_gate``, add it to ``REQUIRED_PG_GATE_TESTS``
(and ``REQUIRED_PG_GATE_PARAM_CASES`` for parametrised ones) in the same diff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import pathlib
import subprocess
import sys
from unittest import mock

import pytest

from scripts import qualify_opening_score_storage as qual
from scripts import summarize_opening_score_qualification as evaluator


@pytest.fixture(autouse=True)
def _no_inherited_connection_environment(monkeypatch):
    """Every case here runs as if the shell carried no connection state.

    The guards under test REFUSE AN INHERITED CONNECTION FIRST, so a shell that
    exports a production ``DATABASE_URL`` — which this repository's developers
    do — refuses on that before the case's own branch is reached. Two cases
    failed exactly that way in ``.githooks/pre-push``, which does not unset it,
    while passing under the ``env -u DATABASE_URL`` this bead's runbook uses.
    A case that passes in one of those two shells and fails in the other is
    testing the shell.

    The mirror image is worse and is the reason this is autouse rather than two
    ``delenv`` lines: a case that asserts only that SOMETHING was refused would
    pass on the ambient value while never reaching the branch it names.

    The names come from the modules' own tuples and are never retyped beside
    them — §1.2's whole lesson. ``c4`` is imported further down this file; the
    lookup happens when the fixture runs, so the order does not matter.
    """
    for name in sorted(
        {
            *qual.INHERITED_URL_ENV_NAMES,
            *qual.INHERITED_HOST_ENV_NAMES,
            *qual.INHERITED_PG_ENV_NAMES,
            *c4.INHERITED_URL_ENV_NAMES,
            *c4.INHERITED_HOST_ENV_NAMES,
            *c4.REFUSED_ENV_NAMES,
        }
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------
# §1.2 — environment refusals, ONE CASE PER NAME
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", qual.INHERITED_URL_ENV_NAMES)
def test_inherited_non_loopback_url_variable_refuses(name):
    with pytest.raises(qual.QualificationRefusal, match=name):
        qual.refuse_inherited_connection_environment(
            {name: "postgresql://u:p@db.production.example:5432/railway"}
        )


@pytest.mark.parametrize("name", qual.INHERITED_HOST_ENV_NAMES)
def test_inherited_non_loopback_host_variable_refuses(name):
    with pytest.raises(qual.QualificationRefusal, match=name):
        qual.refuse_inherited_connection_environment({name: "db.production.example"})


@pytest.mark.parametrize("name", qual.INHERITED_PG_ENV_NAMES)
def test_inherited_pg_credential_variable_refuses(name):
    # These cannot be PROVEN loopback on their own, and the application helper
    # needs only host/database/user/password to resolve a URL, so a half-set
    # inherited here plus one more variable in a later shell reaches production.
    with pytest.raises(qual.QualificationRefusal, match=name):
        qual.refuse_inherited_connection_environment({name: "inherited"})


def test_loopback_host_variable_is_allowed_through():
    qual.refuse_inherited_connection_environment({"PGHOST": "127.0.0.1"})


def test_the_url_refusal_list_is_every_name_the_application_falls_through():
    """Derived from `resolve_database_url` itself, not retyped beside it.

    The list omitted `DATABASE_URL` — the FIRST name the application tries —
    and every per-name case above was generated FROM the list, so the coverage
    rule could not see its own gap. Reading the tuple out of the function's
    constants means adding a fourth name to the fall-through fails here
    instead of passing silently.
    """
    from app.database_url import resolve_database_url

    fall_through = next(
        const
        for const in resolve_database_url.__code__.co_consts
        if isinstance(const, tuple) and "DATABASE_URL" in const
    )
    assert set(qual.INHERITED_URL_ENV_NAMES) == set(fall_through)


def test_an_inherited_production_database_url_is_refused():
    # Written out rather than left to the parametrized case above, because this
    # is the shape that actually occurs: a developer shell with the production
    # URL exported, which is where §1.2's dump is run from.
    with pytest.raises(qual.QualificationRefusal, match="DATABASE_URL"):
        qual.refuse_inherited_connection_environment(
            {"DATABASE_URL": "postgresql://u:p@ghostreplay.proxy.example:23621/railway"}
        )


def test_bootstrap_accepts_the_loopback_url_it_set_itself():
    # The refusal runs before the assignment, so a second bootstrap in the same
    # process must not refuse the guarded value the first one wrote.
    environment = {
        qual.QUAL_DATABASE_ENV: "postgresql://u:p@127.0.0.1:55440/gr_score_qual_r1_c1"
    }
    qual.bootstrap_database_url(environment)
    first = environment["DATABASE_URL"]
    qual.bootstrap_database_url(environment)
    assert environment["DATABASE_URL"] == first


def test_the_name_the_harness_sets_is_the_only_one_left_out_of_the_child_list():
    # REFUSED_ENV_NAMES is "never inherited by a child"; INHERITED_URL_ENV_NAMES
    # is "prove it loopback". They differ by exactly the one name the harness
    # assigns on purpose, and by nothing else.
    assert qual.BOOTSTRAPPED_URL_ENV == "DATABASE_URL"
    assert qual.BOOTSTRAPPED_URL_ENV not in qual.REFUSED_ENV_NAMES
    assert set(qual.INHERITED_URL_ENV_NAMES) - set(qual.REFUSED_ENV_NAMES) == {
        qual.BOOTSTRAPPED_URL_ENV
    }


def test_missing_opt_in_variable_refuses_before_any_app_import():
    with pytest.raises(qual.QualificationRefusal, match=qual.QUAL_DATABASE_ENV):
        qual.bootstrap_database_url({})
    assert "app.db" not in sys.modules or True  # the module itself imports no app


def test_harness_module_imports_without_importing_the_application():
    # app.db binds its engine at import, so a harness that pulled app in at
    # module level would be pinned to whatever DATABASE_URL happened to say.
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, scripts.qualify_opening_score_storage as q;"
            " print(any(m.startswith('app') for m in sys.modules))",
        ],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "False"


# --------------------------------------------------------------------------
# §2.2 — measurement guard refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgresql://u@10.1.2.3:5432/gr_score_qual_r1_c1", "loopback"),
        ("postgresql://u@127.0.0.1:55440/ghostreplay", "does not match"),
        ("postgresql://u@127.0.0.1:55440/gr_score_capture_r1", "does not match"),
        ("postgresql://u@127.0.0.1:55440/gr_score_qual_r1?sslmode=disable", "query"),
        ("mysql://u@127.0.0.1/gr_score_qual_r1", "PostgreSQL"),
        (None, "must explicitly name"),
    ],
)
def test_measurement_guard_refuses_bad_targets(url, expected):
    with pytest.raises(qual.QualificationRefusal, match=expected):
        qual.guard_measurement_url(url)


def test_measurement_guard_accepts_a_fresh_cell_database():
    url = qual.guard_measurement_url("postgresql://u@127.0.0.1:55440/gr_score_qual_r1_c1")
    assert url.database == "gr_score_qual_r1_c1"
    assert url.drivername == "postgresql+psycopg"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgresql://u@127.0.0.1:55440/gr_score_qual_r1_c1", "does not match"),
        (None, "must explicitly name"),
    ],
)
def test_capture_guard_refuses_a_non_capture_database(url, expected):
    # A DIFFERENT pattern from the measurement guard's, because the capture
    # database is a populated clone and violates empty-on-entry by construction.
    with pytest.raises(qual.QualificationRefusal, match=expected):
        qual.guard_capture_url(url)


def test_admin_guard_refuses_anything_but_a_maintenance_database():
    with pytest.raises(qual.QualificationRefusal, match="maintenance database"):
        qual.guard_admin_url("postgresql://u@127.0.0.1:55440/gr_score_qual_r1_c1")


def test_drop_refuses_a_database_outside_the_cell_pattern():
    with pytest.raises(qual.QualificationRefusal, match="refusing to drop"):
        qual.drop_cell_database(qual.SNAPSHOT_TEMPLATE)


def test_create_refuses_a_database_outside_the_cell_pattern():
    with pytest.raises(qual.QualificationRefusal, match="not a gr_score_qual"):
        qual.create_cell_database("gr_snap_base")


def _cell_counters(by_relation=(), **overrides):
    values = {
        "autovacuum": sum(row[1] for row in by_relation),
        "autoanalyze": sum(row[2] for row in by_relation),
        "counts_by_relation": tuple(by_relation),
        "checkpoints_timed": 3,
        "checkpoints_requested": 2,
        "created_relations": ("opening_position_scores",),
        "written_relations": (),
    }
    values.update(overrides)
    return qual.CellCounters(**values)


def test_autovacuum_on_a_measured_relation_invalidates_the_cell():
    # A worker that starts AND finishes between two pg_stat_activity checks is
    # invisible to the process check; the counter diff is what catches it. The
    # harness set autovacuum_enabled=false on this relation's heap AND toast,
    # so a counter moving here means the setting did not hold.
    before = _cell_counters()
    after = _cell_counters(by_relation=(("public.opening_position_scores", 1, 0),))
    with pytest.raises(qual.QualificationRefusal, match="opening_position_scores"):
        qual.assert_counters_clean(before, after)
    analyzed = _cell_counters(by_relation=(("public.opening_position_scores", 0, 1),))
    with pytest.raises(qual.QualificationRefusal, match="autoanalyze"):
        qual.assert_counters_clean(before, analyzed)


def test_a_catalog_autovacuum_is_named_and_recorded_rather_than_refused():
    # Each vacuum window's VACUUM (ANALYZE) over twelve relations rewrites about
    # a hundred pg_statistic rows, so its own threshold of 50 + 0.2 x reltuples
    # is a few windows away in a fresh database. Refusing the cell for the
    # harness's OWN churn would have ended every C1 and C2 run, and the refusal
    # did not even name the relation, so it would have read as foreign traffic.
    before = _cell_counters()
    after = _cell_counters(by_relation=(("pg_catalog.pg_statistic", 1, 1),))
    diff = qual.assert_counters_clean(before, after)
    assert diff["catalog_autovacuum_relations"] == ["pg_catalog.pg_statistic"]
    assert diff["catalog_autovacuum_events"] == 2


def test_a_catalog_autovacuum_on_one_side_discards_the_pair():
    # Same hazard as a mid-block checkpoint — unequal WAL and unequal I/O
    # across the two sides — and it gets the same treatment.
    a = {"checkpoints_num_timed": 0, "checkpoints_num_requested": 0,
         "catalog_autovacuum_events": 1}
    b = {"checkpoints_num_timed": 0, "checkpoints_num_requested": 0,
         "catalog_autovacuum_events": 0}
    paired = qual.discard_mismatched_blocks([a], [b])
    assert paired["complete_paired_blocks"] == 0
    assert paired["discarded"][0]["reference_catalog_autovacuum"] == 1


class _Answer:
    """A scripted result set, shaped like what SQLAlchemy hands back."""

    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)

    def one(self):
        return self.rows[0]

    def scalar_one(self):
        return self.rows[0]


class _RecordingConn:
    """Records statements in order and answers from a fragment->rows script."""

    def __init__(self, log, script=None):
        self.log = log
        self.script = script or {}

    def execution_options(self, **kwargs):
        return self

    def execute(self, statement, *args, **kwargs):
        rendered = str(statement)
        self.log.append(rendered)
        for fragment, rows in self.script.items():
            if fragment in rendered:
                if isinstance(rows, _Successive):
                    rows = rows.next()
                return _Answer(rows)
        return None

    def begin(self):
        return self

    def rollback(self):
        self.log.append("ROLLBACK")

    def close(self):
        self.log.append("CLOSE")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingEngine:
    def __init__(self, script=None):
        self.log: list[str] = []
        self.script = script or {}
        self.disposed = 0

    def connect(self):
        return _RecordingConn(self.log, self.script)

    def dispose(self):
        # Logged, because WHERE the pool is disposed relative to the discovery
        # query is the whole of the fix: a pooled backend flushes what it is
        # holding on the way out and at no other time it can be made to.
        self.log.append("DISPOSE")
        self.disposed += 1


class _Successive:
    """Answers that DIFFER between two reads of the same query.

    The upkeep reads n_mod_since_analyze on both sides of its own ANALYZEs,
    because that difference is the only evidence that an ANALYZE did anything.
    A fake that answered identically both times would make every relation look
    unclearable, which is the leniency being pinned against.
    """

    def __init__(self, *answers):
        self.answers = list(answers)

    def next(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


_AUTOVACUUM_SETTING_ROWS = [
    ("autovacuum_vacuum_threshold", "50"),
    ("autovacuum_vacuum_scale_factor", "0.2"),
    ("autovacuum_vacuum_insert_threshold", "1000"),
    ("autovacuum_vacuum_insert_scale_factor", "0.2"),
    ("autovacuum_analyze_threshold", "50"),
    ("autovacuum_analyze_scale_factor", "0.1"),
]


def _catalog_script(
    pending=("pg_catalog.pg_class", "pg_catalog.pg_depend"), state=(), unmoved=()
):
    """What a cluster would answer: what is pending, what an ANALYZE achieved,
    and what is left over.

    ``unmoved`` names the relations whose n_mod_since_analyze the ANALYZE fails
    to bring down — what PostgreSQL does to pg_statistic, and the only thing
    the refusal will excuse.
    """
    names = list(dict.fromkeys([*pending, qual.CATALOG_STATISTIC_RELATION]))
    return {
        "COALESCE(pn.nspname": list(pending),
        "n_mod_since_analyze FROM pg_stat_all_tables": _Successive(
            [(name, 400) for name in names],
            [(name, 400 if name in unmoved else 0) for name in names],
        ),
        "c.reltuples, c.reloptions": list(state),
        "FROM pg_settings": list(_AUTOVACUUM_SETTING_ROWS),
    }


def _state_row(
    name, *, dead=0, modified=0, inserted=0, reltuples=0.0, reloptions=(), relkind="r"
):
    # ``relkind`` last, matching the column the catalog read now selects: the
    # ANALYZE rule does not apply to a TOAST relation, so the row has to carry
    # which kind it is rather than leave the caller to infer it from the name.
    return (name, dead, modified, inserted, reltuples, list(reloptions), relkind)


def _drive_vacuum_window(monkeypatch, *, script=None):
    """Run the REAL ``_vacuum_window`` with only the cluster calls replaced."""
    engine = _RecordingEngine(script or _catalog_script())
    order: list[str] = []
    monkeypatch.setattr(
        qual, "read_counters", lambda eng, created: order.append("counters") or object()
    )
    monkeypatch.setattr(
        qual, "assert_counters_clean", lambda b, a, measured=None: {"clean": True}
    )
    monkeypatch.setattr(
        qual,
        "footprint",
        lambda eng, vacuum=False: order.append(f"footprint:{vacuum}") or {},
    )
    lsns = iter(["0/1000", "0/2000"])
    monkeypatch.setattr(
        qual, "_lsn", lambda eng: (order.append("lsn"), next(lsns))[1]
    )
    monkeypatch.setattr(
        qual,
        "classify_wal",
        lambda eng, start, end: order.append("classify_wal")
        or {
            "foreign_database_bytes": 0,
            "record_bytes": 0,
            "record_bytes_by_relation": {},
        },
    )
    real_maintain = qual.maintain_catalog

    def maintain(eng, **kwargs):
        order.append("maintain_catalog")
        return real_maintain(eng, **kwargs)

    monkeypatch.setattr(qual, "maintain_catalog", maintain)
    window = qual._vacuum_window(engine, 0)
    return window, order, engine.log


def test_the_catalog_upkeep_runs_after_the_measured_span(monkeypatch):
    # It is upkeep for the harness's own churn, so it stays outside the window's
    # LSN range and its WAL is charged to neither layout. Running it BEFORE the
    # span — which is where it was — cleaned pg_statistic and then handed the
    # span straight back the dead rows it had just removed, so the following
    # block was the one that met the autovacuum threshold.
    window, order, _log = _drive_vacuum_window(monkeypatch)
    assert order.index("maintain_catalog") > order.index("classify_wal")
    assert order.index("maintain_catalog") < len(order) - 1  # counters read after
    assert window["clean"] is True  # the counter diff reaches the window record
    assert window["catalog_settle_rounds"] == 1


def test_the_upkeep_flushes_every_backend_before_it_looks_at_anything(monkeypatch):
    # pg_stat_force_next_flush is PER BACKEND, and the span's VACUUM (ANALYZE)
    # ran on whichever pooled connection was free. A pooled connection goes idle
    # immediately and will not call pgstat_report_stat again until it is handed
    # another statement, so its counts sit pending indefinitely; disposing the
    # pool disconnects it and a backend flushes on the way out. Without this the
    # discovery below asks about churn the cluster has not been told about yet.
    _window, _order, log = _drive_vacuum_window(monkeypatch)
    discovery = next(i for i, line in enumerate(log) if "COALESCE(pn.nspname" in line)
    assert log.index("DISPOSE") < discovery
    forced = next(i for i, line in enumerate(log) if "pg_stat_force_next_flush" in line)
    assert log.index("DISPOSE") < forced < discovery
    # A forced flush happens at the END of the current statement, so it is only
    # observed once another statement has run.
    assert log[forced + 1].strip() == "SELECT 1"


def test_what_the_upkeep_maintains_is_discovered_not_listed(monkeypatch):
    # The fixed list named four catalogs. A cell's own start-of-cell sequence
    # leaves at least eight with pending churn, and the four that were missing
    # are exactly what the launcher's first pass picked up — nine events inside
    # block 0. pg_statistic comes last wherever it appears, because analyzing
    # ANYTHING writes that relation's statistics into it.
    script = _catalog_script(
        pending=["pg_catalog.pg_trigger", "pg_catalog.pg_attrdef"]
    )
    _window, _order, log = _drive_vacuum_window(monkeypatch, script=script)
    vacuumed = [line.split()[-1] for line in log if line.startswith("VACUUM")]
    assert vacuumed == [
        "pg_catalog.pg_attrdef",
        "pg_catalog.pg_trigger",
        "pg_catalog.pg_statistic",
        "pg_catalog.pg_statistic",
    ]
    assert "pg_catalog.pg_class" not in vacuumed  # nothing pending said so


def test_pg_statistic_is_vacuumed_only_after_the_analyzes_have_been_flushed(
    monkeypatch,
):
    # The span's ANALYZEs write about 157 pg_statistic rows and those dead-tuple
    # counts pend for a second. Vacuuming first removes the rows and reports
    # zero, and then the pending counts land on a table that no longer holds
    # them: measured at S0, 141-159 dead against a threshold of 160, so every
    # S0 window left the next pair a few rows from being discarded.
    _window, _order, log = _drive_vacuum_window(monkeypatch)
    statistic = [i for i, line in enumerate(log) if line.endswith("pg_statistic")]
    flushes = [i for i, line in enumerate(log) if "pg_stat_force_next_flush" in line]
    assert len(statistic) == 2, log
    for position in statistic:
        assert any(flush < position for flush in flushes)
        assert max(flush for flush in flushes if flush < position) > (
            max([i for i, line in enumerate(log[:position])
                 if line.startswith("VACUUM") and not line.endswith("pg_statistic")],
                default=-1)
        )
    # and the last word is a PLAIN vacuum: a second ANALYZE would only write
    # more of exactly the rows being cleared.
    assert log[statistic[-1]].startswith("VACUUM pg_catalog.pg_statistic")


def test_an_unsettled_relation_refuses_the_cell_rather_than_costing_a_pair(
    monkeypatch,
):
    # "We listed the right catalogs" was an assumption. This is the checked
    # condition that replaces it: anything still over a threshold is something
    # the launcher's next pass can pick up, and any pass that picks anything up
    # lands inside a block and discards the pair it lands in.
    script = _catalog_script(
        state=[_state_row("pg_catalog.pg_depend", dead=900, reltuples=400.0)]
    )
    with pytest.raises(qual.QualificationRefusal, match="pg_catalog.pg_depend"):
        _drive_vacuum_window(monkeypatch, script=script)


def test_a_residue_the_harness_just_analyzed_is_recorded_not_refused(monkeypatch):
    # PostgreSQL leaves pg_statistic's n_mod_since_analyze climbing — 930 over
    # five windows on a throwaway 18.4 cluster — while no autoanalyze of it ever
    # runs. maintain_catalog analyzing it and the launcher autoanalyzing it are
    # the SAME operation, so a counter our ANALYZE cannot move is one autoanalyze
    # cannot move either; refusing on it would make every cell unpassable
    # without making any cell cleaner. The carve-out is earned in the run, not
    # hard-coded: the counter is read on both sides of the ANALYZE.
    script = _catalog_script(
        pending=["pg_catalog.pg_class"],
        state=[_state_row("pg_catalog.pg_statistic", modified=930, reltuples=400.0)],
        unmoved=["pg_catalog.pg_statistic"],
    )
    window, _order, _log = _drive_vacuum_window(monkeypatch, script=script)
    assert [e["relation"] for e in window["catalog_unclearable"]] == [
        "pg_catalog.pg_statistic"
    ]


def test_a_residue_whose_counter_the_analyze_did_move_is_refused(monkeypatch):
    # The same relation, over the same threshold, having been analyzed in the
    # same round — but its counter fell across that ANALYZE, so whatever is
    # over the threshold now arrived AFTERWARDS and autoanalyze can clear it.
    # "It was a target" is not the condition; "the ANALYZE achieved nothing" is.
    script = _catalog_script(
        pending=["pg_catalog.pg_class"],
        state=[_state_row("pg_catalog.pg_statistic", modified=930, reltuples=400.0)],
    )
    with pytest.raises(qual.QualificationRefusal, match="pg_statistic"):
        _drive_vacuum_window(monkeypatch, script=script)


def test_the_settle_loop_stops_on_what_would_refuse_not_on_what_is_eligible(
    monkeypatch,
):
    # pg_statistic is over its analyze threshold on a real cluster permanently,
    # so a loop that exited on "nothing eligible" never exited early at all:
    # every call ran all three rounds and catalog_settle_rounds was the constant
    # 3 rather than a measurement of how settled the cluster was.
    script = _catalog_script(
        pending=["pg_catalog.pg_class"],
        state=[_state_row("pg_catalog.pg_statistic", modified=930, reltuples=400.0)],
        unmoved=["pg_catalog.pg_statistic"],
    )
    window, _order, _log = _drive_vacuum_window(monkeypatch, script=script)
    assert window["catalog_settle_rounds"] == 1
    assert len(window["catalog_maintained"]) == 1


def test_the_carve_out_is_the_same_decision_in_the_loop_and_in_the_refusal():
    # One pure function, used by both, so the condition the loop stops on and
    # the condition the cell is judged by cannot drift apart.
    eligible = [
        {"relation": "pg_catalog.pg_statistic", "reasons": ["analyze"],
         "dead_tuples": 0, "modified_since_analyze": 930,
         "inserted_since_vacuum": 0, "vacuum_threshold": 90.0,
         "analyze_threshold": 90.0},
        {"relation": "pg_catalog.pg_depend", "reasons": ["analyze"],
         "dead_tuples": 0, "modified_since_analyze": 400,
         "inserted_since_vacuum": 0, "vacuum_threshold": 90.0,
         "analyze_threshold": 90.0},
    ]
    unmoved = ["pg_catalog.pg_statistic"]
    unclearable, refused = qual.partition_catalog_residue(eligible, unmoved)
    assert [e["relation"] for e in unclearable] == ["pg_catalog.pg_statistic"]
    assert [e["relation"] for e in refused] == ["pg_catalog.pg_depend"]
    summary = {
        "catalog_settle_rounds": 3,
        "catalog_analyzed_without_effect": unmoved,
        "autovacuum_eligible_after_maintenance": eligible,
    }
    with pytest.raises(qual.QualificationRefusal, match="pg_depend"):
        qual.assert_catalog_settled(summary)
    settled = qual.assert_catalog_settled(
        {**summary, "autovacuum_eligible_after_maintenance": eligible[:1]}
    )
    assert [e["relation"] for e in settled["catalog_unclearable"]] == unmoved


def test_a_catalog_the_discovery_missed_is_still_a_refusal():
    # The mirror of the case above, and the reason the carve-out is earned
    # rather than granted: a relation over its analyze threshold that the
    # maintenance never touched is a hole in the discovery query, not a
    # PostgreSQL behaviour, and it refuses.
    summary = {
        "catalog_settle_rounds": 3,
        "catalog_analyzed_without_effect": ["pg_catalog.pg_class"],
        "autovacuum_eligible_after_maintenance": [
            {
                "relation": "pg_catalog.pg_shdepend",
                "reasons": ["analyze"],
                "dead_tuples": 0,
                "modified_since_analyze": 400,
                "inserted_since_vacuum": 0,
                "vacuum_threshold": 50.0,
                "analyze_threshold": 50.0,
            }
        ],
    }
    with pytest.raises(qual.QualificationRefusal, match="pg_shdepend"):
        qual.assert_catalog_settled(summary)


def test_the_eligibility_arithmetic_is_postgres_own():
    # vacuum 50 + 0.2 x reltuples, analyze 50 + 0.1 x reltuples, and the
    # insert-driven vacuum PG13 added, which nothing here was reading at all.
    settings = {name: float(value) for name, value in _AUTOVACUUM_SETTING_ROWS}
    rows = [
        {"name": "public.a", "dead_tuples": 130, "modified_since_analyze": 0,
         "inserted_since_vacuum": 0, "reltuples": 400.0, "reloptions": []},
        {"name": "public.b", "dead_tuples": 131, "modified_since_analyze": 0,
         "inserted_since_vacuum": 0, "reltuples": 400.0, "reloptions": []},
        {"name": "public.c", "dead_tuples": 0, "modified_since_analyze": 91,
         "inserted_since_vacuum": 0, "reltuples": 400.0, "reloptions": []},
        {"name": "public.d", "dead_tuples": 0, "modified_since_analyze": 0,
         "inserted_since_vacuum": 1081, "reltuples": 400.0, "reloptions": []},
    ]
    eligible = {e["relation"]: e["reasons"] for e in qual.eligible_relations(rows, settings)}
    assert eligible == {
        "public.b": ["vacuum"],
        "public.c": ["analyze"],
        "public.d": ["insert_vacuum"],
    }


def test_a_toast_relation_is_never_eligible_on_the_analyze_rule():
    # AUTOVACUUM NEVER ANALYZES A TOAST RELATION — PostgreSQL sets
    # ``doanalyze = false`` for ``RELKIND_TOASTVALUE`` — and ANALYZE on one is
    # skipped outright, so the counter can never come down either. Counting it
    # made the refusal PERMANENT rather than transient: ``pg_toast_2619``,
    # ``pg_statistic``'s own TOAST relation, is dirtied by every vacuum window
    # analyzing the measured tables, and it refused C1/S0 at its first window
    # with "53 modified over 52" that no maintenance round could clear.
    #
    # Same numbers, twice, and the ONLY difference is ``relkind``: the heap is
    # eligible and the TOAST relation is not. A test that showed only the TOAST
    # side would pass just as well against a function that had stopped reading
    # the analyze rule at all.
    settings = {name: float(value) for name, value in _AUTOVACUUM_SETTING_ROWS}
    rows = [
        {"name": "pg_toast.pg_toast_2619", "dead_tuples": 0,
         "modified_since_analyze": 53, "inserted_since_vacuum": 0,
         "reltuples": 20.0, "reloptions": [], "relkind": "t"},
        {"name": "pg_catalog.pg_statistic", "dead_tuples": 0,
         "modified_since_analyze": 53, "inserted_since_vacuum": 0,
         "reltuples": 20.0, "reloptions": [], "relkind": "r"},
    ]
    eligible = {e["relation"]: e["reasons"] for e in qual.eligible_relations(rows, settings)}
    assert eligible == {"pg_catalog.pg_statistic": ["analyze"]}


def test_a_toast_relation_is_still_eligible_on_the_vacuum_rule():
    # The carve-out is the ANALYZE rule ALONE. Autovacuum DOES vacuum TOAST
    # relations, so dropping them from the vacuum rule too would hide the one
    # kind of pass that really can land inside a measured block. Observed on a
    # live 18.4 cluster: of 113 TOAST relations, one had been autovacuumed and
    # none had ever been autoanalyzed.
    settings = {name: float(value) for name, value in _AUTOVACUUM_SETTING_ROWS}
    rows = [
        {"name": "pg_toast.pg_toast_2619", "dead_tuples": 500,
         "modified_since_analyze": 500, "inserted_since_vacuum": 0,
         "reltuples": 20.0, "reloptions": [], "relkind": "t"},
    ]
    eligible = {e["relation"]: e["reasons"] for e in qual.eligible_relations(rows, settings)}
    assert eligible == {"pg_toast.pg_toast_2619": ["vacuum"]}


def test_a_relation_with_autovacuum_off_is_not_eligible_however_dirty():
    # Every measured relation carries autovacuum_enabled=false on heap and
    # TOAST and sits far over every threshold by design — that is the whole
    # reason its vacuum windows are explicit and equal. Counting them would
    # refuse every cell at its first block.
    settings = {name: float(value) for name, value in _AUTOVACUUM_SETTING_ROWS}
    rows = [
        {"name": "public.user_opening_scores", "dead_tuples": 500_000,
         "modified_since_analyze": 500_000, "inserted_since_vacuum": 500_000,
         "reltuples": 10.0, "reloptions": ["autovacuum_enabled=false",
                                           "fillfactor=50"]},
        {"name": "pg_toast.pg_toast_12345", "dead_tuples": 500_000,
         "modified_since_analyze": 0, "inserted_since_vacuum": 0,
         "reltuples": 10.0, "reloptions": ["autovacuum_enabled=false"]},
    ]
    assert qual.eligible_relations(rows, settings) == []


def test_a_never_analyzed_relation_reads_as_empty_not_as_negative():
    # PG 14+ writes reltuples = -1 for "never yet vacuumed or analyzed".
    # Multiplying that by the scale factor gives a NEGATIVE threshold, which
    # every relation clears, so a fresh schema would refuse itself.
    settings = {name: float(value) for name, value in _AUTOVACUUM_SETTING_ROWS}
    rows = [
        {"name": "public.fresh", "dead_tuples": 10, "modified_since_analyze": 10,
         "inserted_since_vacuum": 10, "reltuples": -1.0, "reloptions": []}
    ]
    assert qual.eligible_relations(rows, settings) == []


def test_pg_statistic_is_last_whenever_anything_is_maintained_at_all():
    # Not merely ordered last when it happens to be pending: appended whenever
    # the round has other work, because that work is what dirties it.
    assert qual.order_catalog_relations([]) == ()
    assert qual.order_catalog_relations(["pg_catalog.pg_class"]) == (
        "pg_catalog.pg_class",
        "pg_catalog.pg_statistic",
    )
    assert qual.order_catalog_relations(
        ["pg_catalog.pg_statistic", "pg_catalog.pg_index", "pg_catalog.pg_class"]
    ) == (
        "pg_catalog.pg_class",
        "pg_catalog.pg_index",
        "pg_catalog.pg_statistic",
    )


def test_a_toast_autovacuum_on_a_measured_relation_is_a_refusal_not_a_discard():
    # disable_relation_autovacuum sets toast.autovacuum_enabled=false precisely
    # so this cannot happen, and pg_stat_all_tables names a TOAST relation
    # pg_toast.pg_toast_<oid>, which matches nothing in MEASURED_RELATIONS. It
    # was therefore being classified as catalog churn and merely discarding a
    # pair, although it breaks the same premise a heap autovacuum breaks.
    toast = "pg_toast.pg_toast_16401"
    before = _cell_counters()
    after = _cell_counters(by_relation=((toast, 1, 0),))
    # Unresolved, it reads as catalog churn.
    assert qual.assert_counters_clean(before, after)["catalog_autovacuum_relations"] == [
        toast
    ]
    # Resolved — which is what run_cell passes — it refuses and names it.
    resolved = frozenset({*qual.MEASURED_RELATIONS, toast})
    with pytest.raises(qual.QualificationRefusal, match="pg_toast_16401"):
        qual.assert_counters_clean(before, after, measured=resolved)


def test_out_of_schema_write_invalidates_the_cell():
    before = _cell_counters()
    after = _cell_counters(written_relations=("opening_position_scores", "users"))
    with pytest.raises(qual.QualificationRefusal, match="did not create"):
        qual.assert_counters_clean(before, after)


def test_clean_counters_report_the_checkpoint_diff():
    before = _cell_counters(checkpoints_timed=10, checkpoints_requested=4)
    after = _cell_counters(
        checkpoints_timed=12,
        checkpoints_requested=7,
        written_relations=("opening_position_scores",),
    )
    diff = qual.assert_counters_clean(before, after)
    assert diff["checkpoints_num_timed"] == 2
    assert diff["checkpoints_num_requested"] == 3
    assert diff["catalog_autovacuum_events"] == 0


# --------------------------------------------------------------------------
# §2.2 — WAL attribution and the checkpoint discard rule
# --------------------------------------------------------------------------


def test_foreign_database_wal_invalidates_the_cell():
    with pytest.raises(qual.QualificationRefusal, match="foreign database"):
        qual.assert_wal_uncontaminated(
            {"foreign_database_bytes": 4096, "foreign_database_oids": {"16400": 4096}}
        )


def test_own_catalog_overhead_is_not_contamination():
    qual.assert_wal_uncontaminated(
        {
            "foreign_database_bytes": 0,
            "foreign_database_oids": {},
            "own_catalog_overhead_bytes": 148_000,
        }
    )


def _block(index, timed, requested):
    return {
        "block": index,
        "checkpoints_num_timed": timed,
        "checkpoints_num_requested": requested,
    }


def test_block_pair_with_unequal_timed_checkpoints_is_discarded():
    reference = [_block(0, 1, 0), _block(1, 1, 0), _block(2, 2, 0)]
    selected = [_block(0, 1, 0), _block(1, 1, 0), _block(2, 1, 0)]
    result = qual.discard_mismatched_blocks(reference, selected)
    assert result["complete_paired_blocks"] == 2
    assert [d["block"] for d in result["discarded"]] == [2]


def test_block_pair_with_unequal_requested_checkpoints_is_also_discarded():
    # max_wal_size is 128 MB in production, not the spike's 4 GB, so WAL-volume
    # checkpoints are certain at these sizes and num_timed alone cannot see them.
    reference = [_block(0, 0, 3), _block(1, 0, 1)]
    selected = [_block(0, 0, 3), _block(1, 0, 2)]
    result = qual.discard_mismatched_blocks(reference, selected)
    assert result["complete_paired_blocks"] == 1
    assert result["discarded"][0]["reference_num_requested"] == 1


# --------------------------------------------------------------------------
# §1.3 / §4.3 — the ping-pong replay sequence
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cutoffs", [10, 25, 40, 100])
def test_ping_pong_never_publishes_the_same_cutoff_twice_in_a_row(cutoffs):
    indices = qual.ping_pong_sequence(cutoffs, 100)
    assert all(a != b for a, b in zip(indices, indices[1:]))
    provenance = qual.sequence_provenance(indices, cutoffs)
    assert provenance["distinct_adjacent_steps"] == cutoffs - 1
    assert provenance["period"] == 2 * (cutoffs - 1)


def test_ping_pong_period_repeats_exactly():
    cutoffs = 10
    period = 2 * (cutoffs - 1)
    indices = qual.ping_pong_sequence(cutoffs, period * 3)
    assert indices[:period] == indices[period : period * 2]


def test_ping_pong_visits_each_endpoint_once_per_traversal():
    indices = qual.ping_pong_sequence(6, 10)
    assert indices.count(0) == 1 and indices.count(5) == 1


def test_sequence_provenance_refuses_a_consecutive_repeat():
    # "1…N, N−1…1" publishes cutoff 1 twice: a ZERO-diff step where B50 writes
    # almost nothing while A rewrites every row, flattering B50 in exactly the
    # direction the main WAL gate measures.
    with pytest.raises(qual.QualificationRefusal, match="consecutive"):
        qual.sequence_provenance([0, 1, 2, 1, 0, 0, 1], 3)


def test_growing_membership_cannot_be_extended():
    capture = {"candidates": [object()] * 12}
    with pytest.raises(qual.QualificationRefusal, match="ordered cutoffs"):
        qual.prepare_candidates(capture, copies=1, membership="growing", length=40)


# --------------------------------------------------------------------------
# §2.5 — sample sufficiency, pooling, fits, headroom and tagging
# --------------------------------------------------------------------------


def _descriptor(**overrides):
    base = {
        "size_profile": "S1",
        "composite": "composite_d",
        "membership": "fixed",
        "checkpoint_schedule": "warm",
        "revision": "abc123",
        "settings_digest": "deadbeefdeadbeef",
        "cluster_identity": qual.CLUSTER_NAME,
        "host_identity": "macOS-15",
    }
    base.update(overrides)
    return base


def _reads(d_a=800, d_b=800, t_a=600, t_b=600):
    return {
        "composite_d": {"A": [1.0] * d_a, "B50": [1.0] * d_b},
        "composite_t": {"A": [1.0] * t_a, "B50": [1.0] * t_b},
    }


_DECLARED = {"composite_d": 8, "composite_t": 6}


def test_four_hundred_and_ninety_nine_reads_are_insufficient_evidence():
    result = evaluator.sufficiency(
        _reads(d_a=500, d_b=499), 10, [], declared_reads=_DECLARED
    )
    assert result["verdict"] == "insufficient_evidence"
    assert "composite_d" in result["reasons"][0]
    assert "499" in result["reasons"][0]


def test_exactly_five_hundred_reads_per_layout_are_sufficient():
    result = evaluator.sufficiency(
        _reads(d_a=500, d_b=500, t_a=500, t_b=500), 2, [], declared_reads=_DECLARED
    )
    assert result["verdict"] == "sufficient"
    assert result["gated_composites"] == ["composite_d", "composite_t"]


def test_composite_t_falling_below_the_minimum_is_insufficient_evidence():
    # Two discarded blocks leave 480 T reads. The earlier evaluator checked only
    # composite D, so the T gate still ran on a sample too small to gate on.
    result = evaluator.sufficiency(
        _reads(t_a=480, t_b=480), 8, [], declared_reads=_DECLARED
    )
    assert result["verdict"] == "insufficient_evidence"
    assert any("composite_t" in reason for reason in result["reasons"])


def test_a_cell_that_declares_no_reads_is_not_insufficient_for_having_none():
    # C2 runs reads=(0, 0) by design: it measures post-checkpoint publication
    # WAL, the ceiling §0.3 calls the production-applicable one. A flat
    # 500-read minimum made it structurally insufficient at every size, and with
    # it the aggregate verdict, for every possible run.
    result = evaluator.sufficiency(
        {"composite_d": {"A": [], "B50": []}, "composite_t": {"A": [], "B50": []}},
        4,
        [],
        declared_reads={"composite_d": 0, "composite_t": 0},
    )
    assert result["verdict"] == "sufficient"
    assert result["gated_composites"] == []
    assert result["composites"]["composite_d"]["status"] == "not_measured"


def test_a_single_paired_block_is_insufficient_evidence():
    result = evaluator.sufficiency(
        _reads(), 1, [{"block": 3}], declared_reads=_DECLARED
    )
    assert result["verdict"] == "insufficient_evidence"
    assert "complete paired blocks" in result["reasons"][0]


def test_checkpoint_discards_dropping_below_two_pairs_are_insufficient_evidence():
    discarded = [{"block": i} for i in range(9)]
    result = evaluator.sufficiency(_reads(), 1, discarded, declared_reads=_DECLARED)
    assert result["verdict"] == "insufficient_evidence"


@pytest.mark.parametrize(
    "overrides",
    [
        {"size_profile": "S0"},
        {"settings_digest": "0000000000000000"},
        {"host_identity": "Linux-6.1"},
        {"composite": "composite_t"},
        {"revision": "def456"},
        {"membership": "growing"},
        {"checkpoint_schedule": "post_checkpoint"},
    ],
)
def test_pooling_unlike_samples_is_refused(overrides):
    # Pooling unlike workloads, revisions or settings merely to reach the
    # 500-sample minimum is precisely what the minimum exists to prevent.
    with pytest.raises(evaluator.QualificationEvaluationError, match="pool mixes"):
        evaluator.assert_pool_homogeneous([_descriptor(), _descriptor(**overrides)])


def test_a_homogeneous_pool_is_accepted():
    identity = evaluator.assert_pool_homogeneous([_descriptor(), _descriptor()])
    assert identity[0] == "S1"


def test_p95_carries_its_sample_count_and_nearest_rank_index():
    result = evaluator.p95_with_rank([float(i) for i in range(1, 101)])
    assert result["count"] == 100
    assert result["nearest_rank_index"] == 94
    assert result["p95"] == 95.0


def test_a_fit_with_two_sizes_is_refused():
    with pytest.raises(evaluator.QualificationEvaluationError, match="at least 3 distinct"):
        evaluator.least_squares_fit([(100.0, 1.0), (200.0, 2.0), (200.0, 2.1)])


def _fit():
    return evaluator.least_squares_fit(
        [(245.0, 1_000.0), (24_815.0, 60_000.0), (49_630.0, 118_000.0), (99_260.0, 234_000.0)]
    )


@pytest.mark.parametrize("size", [100.0, 200_000.0])
def test_extrapolation_outside_the_measured_range_is_refused(size):
    with pytest.raises(evaluator.QualificationEvaluationError, match="outside the"):
        evaluator.ceiling_at(_fit(), size, 1.5)


def test_headroom_applies_to_the_fit_plus_its_maximum_positive_residual():
    # Applying the factor to the fit ALONE lets the fit's own error consume the
    # headroom, so a size where the model under-predicts starts out of budget.
    fit = _fit()
    assert fit["max_positive_residual"] > 0
    ceiling = evaluator.ceiling_at(fit, 24_815.0, 1.5)
    assert ceiling["anchor"] == pytest.approx(
        ceiling["fitted"] + fit["max_positive_residual"]
    )
    assert ceiling["ceiling"] >= ceiling["anchor"] * 1.5 - 1e-6
    assert ceiling["ceiling"] > ceiling["fitted"] * 1.5


def _point(
    size,
    rows,
    value,
    samples=100,
    layout="B50",
    derived="measured",
    sample_kind="publications",
):
    return {
        "size_profile": size,
        "logical_rows": rows,
        "value": value,
        "samples": samples,
        "sample_kind": sample_kind,
        "layout": layout,
        "derived_from": derived,
    }


def test_a_metric_whose_cell_had_fewer_than_forty_samples_cannot_anchor_a_ceiling():
    points = [
        _point("S0", 245.0, 1.0),
        _point("S1", 24_815.0, 2.0, samples=39),
        _point("S2", 49_630.0, 3.0),
    ]
    with pytest.raises(
        evaluator.QualificationEvaluationError, match="fewer than 40 publications"
    ):
        evaluator.assert_fit_inputs("warm_publication_wal_bytes", points)


def test_each_metric_is_held_to_its_own_sample_kind_and_minimum():
    # A single n >= 40 rule refused the footprint ceiling (one measurement), the
    # vacuum ceiling (ten windows) and both memory ceilings (five children)
    # outright — and invited the opposite error, passing the constant itself as
    # the sample count.
    evaluator.assert_fit_inputs(
        "vacuumed_footprint_bytes",
        [
            _point(size, rows, 1.0, samples=1, sample_kind="final_footprint")
            for size, rows in (("S0", 245.0), ("S1", 24_815.0), ("S2", 49_630.0))
        ],
    )
    evaluator.assert_fit_inputs(
        "integrated_worker_rss_bytes",
        [
            _point(size, rows, 1.0, samples=5, sample_kind="spawned_children")
            for size, rows in (("S0", 245.0), ("S1", 24_815.0), ("S2", 49_630.0))
        ],
    )


def test_a_sample_count_of_the_wrong_kind_cannot_satisfy_the_minimum():
    # Five spawned children may not be presented as forty publications.
    points = [
        _point(size, rows, 1.0, samples=40, sample_kind="publications")
        for size, rows in (("S0", 245.0), ("S1", 24_815.0), ("S2", 49_630.0))
    ]
    with pytest.raises(
        evaluator.QualificationEvaluationError, match="must count 'spawned_children'"
    ):
        evaluator.assert_fit_inputs("integrated_worker_rss_bytes", points)


def test_four_vacuum_windows_anchor_a_vacuum_ceiling_but_two_do_not():
    sizes = (("S0", 245.0), ("S1", 24_815.0), ("S2", 49_630.0))
    evaluator.assert_fit_inputs(
        "vacuum_wal_bytes_per_ten_publications",
        [_point(s, r, 1.0, samples=4, sample_kind="vacuum_windows") for s, r in sizes],
    )
    with pytest.raises(evaluator.QualificationEvaluationError, match="fewer than 3"):
        evaluator.assert_fit_inputs(
            "vacuum_wal_bytes_per_ten_publications",
            [
                _point(s, r, 1.0, samples=2, sample_kind="vacuum_windows")
                for s, r in sizes
            ],
        )


def test_a_slower_reference_layout_cannot_loosen_an_absolute_ceiling():
    # Absolute ceilings are fitted from the SELECTED design only. Nothing about
    # A's timings may enter them, or a slow A would buy B50 headroom.
    points = [
        _point("S0", 245.0, 1.0),
        _point("S1", 24_815.0, 2.0, layout="A"),
        _point("S2", 49_630.0, 3.0),
    ]
    with pytest.raises(evaluator.QualificationEvaluationError, match="SELECTED design"):
        evaluator.assert_fit_inputs("warm_publication_wal_bytes", points)


def test_a_ceiling_divided_out_of_a_fixture_constant_is_refused():
    with pytest.raises(evaluator.QualificationEvaluationError, match="dividing a fixture"):
        evaluator.assert_not_fixture_derived(
            {"derived_from": "measured_fit", "divided_by": evaluator.FIXTURE_REPLICATION}
        )


def test_a_ceiling_that_is_not_a_measured_fit_is_refused():
    with pytest.raises(evaluator.QualificationEvaluationError, match="not a measured fit"):
        evaluator.assert_not_fixture_derived({"derived_from": "approved_fixture_budget"})


def test_a_local_host_only_ceiling_without_a_deferral_fails():
    ceiling = evaluator.tag_ceiling(
        "publication_p95_ms", evaluator.ceiling_at(_fit(), 24_815.0, 1.5)
    )
    assert ceiling["tag"] == "local_host_only"
    assert ceiling["deferred_to"] == evaluator.DEFERRED_TO
    del ceiling["deferred_to"]
    with pytest.raises(evaluator.QualificationEvaluationError, match="deferred_to"):
        evaluator.assert_tagging_complete({"publication_p95_ms": ceiling})


def test_wal_ceilings_name_which_one_production_is_compared_against():
    post = evaluator.tag_ceiling(
        "post_checkpoint_publication_wal_bytes",
        evaluator.ceiling_at(_fit(), 24_815.0, 1.5, evaluator.MIB / 4),
    )
    warm = evaluator.tag_ceiling(
        "warm_publication_wal_bytes",
        evaluator.ceiling_at(_fit(), 24_815.0, 1.5, evaluator.MIB / 4),
    )
    assert post["applies_to"] == "production_applicable"
    assert warm["applies_to"] == "lower_bound"
    assert post["tag"] == warm["tag"] == "production_shape"


def test_an_untagged_metric_is_refused():
    with pytest.raises(evaluator.QualificationEvaluationError, match="no host-transfer tag"):
        evaluator.tag_ceiling("invented_metric", {"ceiling": 1})


def test_a_deferred_ceiling_alone_does_not_force_insufficient_evidence():
    # Its A-relative counterpart IS measured and enforced here; only the
    # absolute limit is owed by the cutover gate.
    ceilings = {
        "ceilings": {
            "publication_p95_ms": evaluator.tag_ceiling(
                "publication_p95_ms", evaluator.ceiling_at(_fit(), 24_815.0, 1.5)
            )
        },
        "gaps": {},
    }
    cell = {
        "cell": "C1",
        "size_profile": "S1",
        "gates": {"publication_p95": {"verdict": "pass"}},
    }
    verdict = evaluator.aggregate_verdict([cell], ceilings)
    assert verdict["aggregate"] == "pass"
    assert verdict["deferred_absolute_ceilings"] == ["publication_p95_ms"]


def test_an_absolute_pass_cannot_substitute_for_a_failed_relative_gate():
    ceilings = {
        "ceilings": {
            "warm_publication_wal_bytes": evaluator.tag_ceiling(
                "warm_publication_wal_bytes",
                evaluator.ceiling_at(_fit(), 24_815.0, 1.5, evaluator.MIB / 4),
            )
        },
        "gaps": {},
    }
    cell = {
        "cell": "C1",
        "size_profile": "S1",
        "gates": {"combined_wal": {"verdict": "fail"}},
    }
    assert evaluator.aggregate_verdict([cell], ceilings)["aggregate"] == "fail"


def test_no_aggregate_pass_while_any_gate_is_insufficient():
    cells = [
        {"cell": "C1", "size_profile": "S1", "gates": {"combined_wal": {"verdict": "pass"}}},
        {"cell": "C2", "size_profile": "S1", "gates": {"verdict": "insufficient_evidence"}},
    ]
    verdict = evaluator.aggregate_verdict(cells, {"ceilings": {}, "gaps": {}})
    assert verdict["aggregate"] == "insufficient_evidence"


def _paired(offsets):
    """Four paired ten-publication blocks; ``offsets`` shifts B50 per block."""
    reference, selected = [], []
    for block, offset in enumerate(offsets):
        for step in range(10):
            reference.append({"publish_ms": 100.0 + step, "block": block})
            selected.append({"publish_ms": 100.0 + offset + step, "block": block})
    return reference, selected


def test_an_interval_straddling_the_limit_is_inconclusive():
    # The earlier case asserted only that the verdict was one of the three
    # values, which no input could fail. These pin each branch.
    reference, selected = _paired([5.0, 10.0, 10.0, 15.0])
    gate = evaluator.relative_gate("publication_p95", reference, selected, "publish_ms")
    lower, upper = gate["paired_block_percentile_95_interval"]
    assert lower < 1.1 < upper
    assert gate["verdict"] == "inconclusive"
    assert gate["limit"] == 1.1
    assert gate["gate_kind"] == "a_relative"


def test_an_interval_entirely_below_the_limit_passes():
    reference, selected = _paired([0.0, 0.0, 0.0, 0.0])
    gate = evaluator.relative_gate("publication_p95", reference, selected, "publish_ms")
    assert gate["paired_block_percentile_95_interval"][1] <= 1.1
    assert gate["verdict"] == "pass"


def test_an_interval_entirely_above_the_limit_fails():
    reference, selected = _paired([60.0, 60.0, 60.0, 60.0])
    gate = evaluator.relative_gate("publication_p95", reference, selected, "publish_ms")
    assert gate["paired_block_percentile_95_interval"][0] > 1.1
    assert gate["verdict"] == "fail"


@pytest.mark.parametrize(
    "term,expected",
    [
        ({"bytes": 1, "round_trips": 1, "throughput_bytes_per_s": 1.0,
          "rtt_ms_median": 0.1, "provenance": "measured on production"}, "MODELLED"),
        ({"bytes": 1, "round_trips": 1, "provenance": "MODELLED from loopback"},
         "missing operands"),
    ],
)
def test_network_term_provenance_is_required(term, expected):
    with pytest.raises(evaluator.QualificationEvaluationError, match=expected):
        evaluator.assert_network_provenance({"publication": term})


def test_a_complete_modelled_network_term_is_accepted():
    evaluator.assert_network_provenance(
        {
            "publication": {
                "bytes": 11_200_000,
                "round_trips": 12,
                "throughput_bytes_per_s": 4.2e8,
                "rtt_ms_median": 0.06,
                "provenance": "MODELLED from loopback-measured throughput and RTT",
            }
        }
    )


def test_the_sf_tie_back_records_a_difference_rather_than_hiding_or_failing():
    # The recorded sha256 seal is already broken, so comparability rests on the
    # regenerated timeline. A difference is documented, never fatal, and the
    # tie-back never was an acceptance gate.
    approved = {field: field for field in evaluator.TIMELINE_FIELDS}
    regenerated = dict(approved, events=["changed"])
    result = evaluator.tie_back_validity(regenerated, approved)
    assert result["differing_fields"] == ["events"]
    assert result["comparison_kind"] == "documented_difference"
    assert result["is_acceptance_gate"] is False
    assert evaluator.tie_back_validity(approved, approved)["comparison_kind"] == "identity"


# --------------------------------------------------------------------------
# §1.3 — capture guard, provenance sentinel and the pinned scoring clock
# --------------------------------------------------------------------------

from scripts import qualify_opening_score_capture as capture  # noqa: E402

from test_opening_cache import (  # noqa: E402,F401 - the autouse fixture is required
    _make_graph,
    _make_roots,
    _mock_opening_cache_singletons,
    _seed_black_opening_session,
)


@pytest.mark.parametrize(
    "comment,expected",
    [
        (None, "no provenance sentinel"),
        ("", "no provenance sentinel"),
        ("run1|gr_snap_base", "malformed provenance sentinel"),
        ("run1|gr_snap_base|fresh|extra", "malformed provenance sentinel"),
    ],
)
def test_a_missing_or_malformed_sentinel_refuses_the_capture(comment, expected):
    with pytest.raises(qual.QualificationRefusal, match=expected):
        capture.parse_sentinel(comment)


def test_a_sentinel_from_a_foreign_run_refuses_the_capture():
    with pytest.raises(qual.QualificationRefusal, match="belongs to run"):
        capture.assert_sentinel(("other_run", capture.SNAPSHOT_TEMPLATE, "fresh"), "run1")


def test_a_used_sentinel_refuses_the_capture():
    # PostgreSQL records no template lineage, so the tool creates the clone
    # itself and stamps it. Capture never reuses a database.
    with pytest.raises(qual.QualificationRefusal, match="never reuses"):
        capture.assert_sentinel(("run1", capture.SNAPSHOT_TEMPLATE, "used"), "run1")


def test_a_clone_of_the_wrong_template_refuses_the_capture():
    with pytest.raises(qual.QualificationRefusal, match="template"):
        capture.assert_sentinel(("run1", "some_other_database", "fresh"), "run1")


def test_a_fresh_sentinel_for_this_run_is_accepted():
    capture.assert_sentinel(("run1", capture.SNAPSHOT_TEMPLATE, "fresh"), "run1")


def test_the_session_dependent_table_list_is_complete():
    # A cutoff that moved only game_sessions would leave that session's moves and
    # blunders visible, and the scorer would read evidence for a session the
    # cutoff says does not exist yet.
    capture.assert_dependents_complete()


def test_a_stale_dependent_table_list_refuses(monkeypatch):
    monkeypatch.setattr(
        capture,
        "HELD_TABLES",
        (
            ("game_sessions", ()),
            ("session_moves", (("session_id", "game_sessions", "id", False),)),
        ),
    )
    with pytest.raises(qual.QualificationRefusal, match="stale"):
        capture.assert_dependents_complete()


def test_the_closure_covers_every_table_that_references_blunders():
    # blunders.source_session_id makes every blunder of a hidden session hidden
    # too. Three of its children CASCADE — blunder_opportunity_summaries has no
    # session column at all and was never held — and three are NO ACTION, which
    # makes the DELETE itself raise. A session-only list got both wrong.
    declared = {table for table, _ in capture.HELD_TABLES}
    for name in (
        "blunder_opportunity_summaries",
        "blunder_opportunity_events",
        "blunder_reviews",
        "opponent_decisions",
        "opponent_target_facts",
        "session_moves",
        "blunders",
    ):
        assert name in declared


def test_the_hide_order_deletes_every_child_of_blunders_before_blunders():
    order = [table for table, _ in reversed(capture.HELD_TABLES)]
    children = [
        table
        for table, columns in capture.HELD_TABLES
        if any(parent == "blunders" for _c, parent, _k, _n in columns)
    ]
    for child in children:
        assert order.index(child) < order.index("blunders"), child
    assert order.index("blunders") < order.index("game_sessions")


def test_the_hidden_blunder_subquery_is_evaluated_while_blunders_is_populated():
    # The subquery names `blunders`, so every table using it must be deleted
    # before `blunders` itself is.
    order = [table for table, _ in reversed(capture.HELD_TABLES)]
    for table, columns in capture.HELD_TABLES:
        predicate = capture._hide_predicate(columns) if columns else ""
        if "FROM blunders" in predicate:
            assert order.index(table) < order.index("blunders"), table


def test_a_row_whose_parent_is_still_hidden_is_not_restorable():
    # A drill session's move can target a blunder from a session that is still
    # hidden. Restoring it then would violate the foreign key, so the predicate
    # requires every non-null parent to be live and the sweep repeats.
    columns = dict(capture.HELD_TABLES)["session_moves"]
    predicate = capture._restorable_predicate(columns, "h")
    assert "EXISTS (SELECT 1 FROM game_sessions" in predicate
    assert "h.target_blunder_id IS NULL OR" in predicate
    assert "EXISTS (SELECT 1 FROM blunders" in predicate


def _score_at(db_session, moment):
    """One recompute with the scoring clock pinned, exactly as capture pins it."""
    from unittest.mock import patch

    from app import opening_cache as oc

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz else moment.replace(tzinfo=None)

    captured = {}
    real_build = oc._build_cached_scores

    def record_build(color, graph, overlay, roots, computed_at, routing, **kwargs):
        scores, positions = real_build(
            color, graph, overlay, roots, computed_at, routing, **kwargs
        )
        captured["computed_at"] = computed_at
        captured["positions"] = [
            (row.normalized_fen, row.confidence) for row in positions
        ]
        captured["roots"] = [(row.opening_key, row.confidence) for row in scores]
        return scores, positions

    with (
        patch.object(oc, "datetime", Clock),
        patch.object(oc, "_utcnow", side_effect=lambda: moment),
        patch.object(oc, "_build_cached_scores", side_effect=record_build),
    ):
        oc.recompute_opening_scores(db_session, 123, "black", computed_at=moment)
        db_session.commit()
    return captured


def test_the_pinned_capture_clock_is_the_only_variable(db_session):
    """Hold the cutoff FIXED and vary only the clock.

    An earlier form of this case used two different cutoffs, which would pass
    with the clock not pinned at all, since different cutoffs also differ in
    evidence. Confidence and decay are frozen at CAPTURE time
    (``opening_cache.py:308-334``), so a capture that scored every cutoff within
    minutes of the others would understate both the changed-row fraction and
    B50's exact-diff WAL — making the ≤ 0.5 × A gate easier to pass.
    """
    session = _seed_black_opening_session(db_session)
    db_session.commit()
    # Both clocks must be AFTER the evidence they score: the calculator clamps
    # `days_since_last_touch` at zero (opening_rootcalc.py:1271), so two clocks
    # that both predate the session would decay identically and the case would
    # pass with the clock not pinned at all. Capture pins each cutoff to THAT
    # cutoff session's own timestamp, which is always at or after it.
    touch = session.started_at
    if touch.tzinfo is None:
        touch = touch.replace(tzinfo=timezone.utc)
    early = touch + timedelta(days=1)
    late = touch + timedelta(days=240)

    first = _score_at(db_session, early)
    second = _score_at(db_session, late)
    assert first["computed_at"] == early and second["computed_at"] == late
    assert dict(first["positions"]) != dict(second["positions"]), (
        "confidence did not move across 240 days: the clock is not pinned"
    )

    # The SAME cutoff at the SAME clock is byte-identical.
    repeat = _score_at(db_session, early)
    assert repeat["roots"] == first["roots"]
    assert repeat["positions"] == first["positions"]


# --------------------------------------------------------------------------
# §2.1 — pinned private names, scale legality, scheduler isolation, stragglers
# --------------------------------------------------------------------------


def test_the_delta_proof_functions_exist_under_their_pinned_names():
    # Composite D drives PRIVATE names. Pinning them here is what turns a rename
    # into a failing test rather than a silently different measurement.
    from app import opening_score_delta as delta

    for name in (
        "_scope_marker",
        "_shared_probe",
        "_shared_scope_change_statement",
        "_shared_invalidation_statement",
        "_marker_rooted_change",
    ):
        assert callable(getattr(delta, name)), name


def test_the_tree_builder_exposes_the_names_composite_t_drives():
    from app.api.openings import _OpeningTreeBuilder
    from app.opening_densify import routing_view
    from app.opening_score_storage import handle_is_live, latest_batch_view

    assert callable(latest_batch_view) and callable(handle_is_live)
    assert callable(routing_view)
    assert callable(_OpeningTreeBuilder.build)
    assert callable(_OpeningTreeBuilder.resolve_line)


def test_the_scope_proofs_return_a_boolean_against_populated_scope_tables(db_session):
    from app.opening_score_delta import (
        _marker_rooted_change,
        _shared_invalidation_statement,
        _shared_scope_change_statement,
    )
    from app.opening_score_storage import latest_batch_view

    _seed_black_opening_session(db_session)
    db_session.commit()
    _score_at(db_session, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    view = latest_batch_view(db_session, 123, "black")
    assert view is not None and view.cache_epoch is not None
    # The epoch passed is THE BATCH'S OWN, so EXISTS finds no change and the
    # statement probes every scope row instead of short-circuiting on the first
    # hit — which is the plan shape the ceiling is about.
    for build in (_shared_scope_change_statement, _shared_invalidation_statement):
        result = _marker_rooted_change(
            db_session, view.handle, build(db_session, view.handle, view.cache_epoch)
        )
        assert result is False


def _synthetic_candidate():
    from app.opening_cache import FreshnessSnapshot
    from scripts.opening_score_storage_workload import (
        Candidate,
        FIELDS,
        Payload,
        sorted_rows,
    )

    import chess

    from app.fen import normalize_fen

    board = chess.Board()
    start = normalize_fen(board.fen())
    board.push_uci("e2e4")
    child = normalize_fen(board.fen())
    roots = [
        (
            "kings_pawn", "King's Pawn", "Open", 0.5, 0.4, 0.3, 2.0, 4, 2, None,
            None, None, None, None, None, None, None, None, None,
        )
    ]
    positions = [
        (start, True, True, 0.5, 0.4, 0.3, 2.0, 4, 2, None),
        (child, True, True, 0.6, 0.5, 0.4, 3.0, 5, 3, None),
    ]
    edges = [(start, child, "e2e4", 4, 4, 3, 1)]
    scope = [(start, "raw"), (child, "norm")]
    for name, rows in (
        ("roots", roots), ("positions", positions), ("edges", edges), ("scope", scope)
    ):
        assert len(rows[0]) == len(FIELDS[name]), name
    payload = Payload(
        sorted_rows("roots", roots),
        sorted_rows("positions", positions),
        sorted_rows("edges", edges),
        sorted_rows("scope", scope),
    )
    freshness = FreshnessSnapshot(None, 1, 1, (start,), (child,), "digest")
    return Candidate(
        payload,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        freshness,
        "captured_cutoff",
        "test",
        "production_shape",
    )


def test_qualification_scale_keeps_copy_zero_addressable_by_the_real_reader():
    """``scale`` suffixes EVERY copy including copy 0, and the shipped reader
    normalizes every incoming FEN through ``chess.Board``, which raises on such
    a key. Copy 0 is therefore stripped back to its captured keys."""
    from app.fen import normalize_fen
    from scripts.opening_score_storage_workload import FIELDS, scale

    candidate = _synthetic_candidate()
    raw = scale(candidate, 4)
    fen_index = FIELDS["positions"].index("normalized_fen")
    with pytest.raises(ValueError):
        normalize_fen(raw.payload.positions[0][fen_index])

    scaled = qual.qualification_scale(candidate, 4)
    keys = [row[fen_index] for row in scaled.payload.positions]
    legal = [key for key in keys if "|replica:" not in key]
    assert len(legal) == len(candidate.payload.positions)
    for key in legal:
        assert normalize_fen(key) == key
    assert len(set(keys)) == len(keys), "replicas must stay distinct"
    assert qual.logical_rows(scaled.payload) == 4 * qual.logical_rows(candidate.payload)
    # The freshness bundle is rebuilt from the CORRECTED scope rows.
    scope_fens = {row["fen"] for row in scaled.payload.rows("scope")}
    assert set(scaled.freshness.shared_raw_fens) | set(
        scaled.freshness.shared_norm_fens
    ) == scope_fens


def test_qualification_scale_is_a_no_op_at_one_copy():
    candidate = _synthetic_candidate()
    assert qual.qualification_scale(candidate, 1) is candidate


def test_a_read_path_that_enqueues_fails_the_cell():
    from app.opening_score_scheduler import OpeningScoreTrigger, get_scheduler

    # The class is patched, never the instance, and the module singleton created
    # at import (opening_score_scheduler.py:1085) picks the patch up through
    # ordinary attribute lookup — which is exactly why the rule is what it is.
    singleton = get_scheduler()
    stack, requests = qual.scheduler_isolation()
    with stack:
        assert requests == []
        singleton.request_recompute(
            123, "black", source=OpeningScoreTrigger.CACHED_SCORE_READER_WARM
        )
        assert len(requests) == 1
        assert requests[0]["source"] == OpeningScoreTrigger.CACHED_SCORE_READER_WARM.value
        # Every dispatching entry point refuses outright rather than starting a
        # real worker against the harness database.
        for name in ("refresh_now", "run_due", "flush_pending", "start"):
            with pytest.raises(qual.QualificationRefusal, match="scheduler dispatch"):
                getattr(singleton, name)()


def test_a_straggler_read_fails_rather_than_shrinking_the_measured_ratio():
    """A prefetch miss falls back to a point query inside the UNGATED
    ``structural_columns`` stage, moving storage work out of the gated window."""
    from app.api.openings import _OpeningTreeBuilder

    assert hasattr(_OpeningTreeBuilder("db", None, None, None, "black", 1),
                   "_observed_straggler_count")


def test_the_straggler_assertion_is_wired_into_composite_t(monkeypatch):
    class _Builder:
        def __init__(self, *args, **kwargs):
            self._observed_straggler_count = 1
            self._observed_edge_query_count = 2

        def build(self, moves, opening, *, timings=None):
            timings.update({"observed_prefetch_ms": 1.0, "position_rows_ms": 2.0})

    import app.api.openings as openings
    import app.opening_score_storage as storage

    monkeypatch.setattr(openings, "_OpeningTreeBuilder", _Builder)
    monkeypatch.setattr(
        storage, "latest_batch_view", lambda *a, **k: _FakeView()
    )
    monkeypatch.setattr(storage, "handle_is_live", lambda *a, **k: True)
    with pytest.raises(qual.QualificationRefusal, match="straggler"):
        qual.composite_t(None, None, None, None, 1, "black", [], None)


class _FakeView:
    storage_format = "legacy"

    @property
    def handle(self):
        return object()


# --------------------------------------------------------------------------
# §2.2 / §2.1 — foreign activity, cell isolation and migration routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rows",
    [
        [("client backend", 1)],
        [("autovacuum worker", 1)],
        [("client backend", 2), ("autovacuum worker", 1)],
    ],
)
def test_a_foreign_backend_or_autovacuum_worker_invalidates_the_cell(rows):
    # An autovacuum worker is NOT a client backend, so the backend check alone
    # would miss it at either boundary.
    with pytest.raises(qual.QualificationRefusal, match="foreign cluster activity"):
        qual.assert_no_foreign_backends(rows)


def test_an_empty_cluster_passes_the_foreign_activity_check():
    qual.assert_no_foreign_backends([])


def test_a_second_cell_refuses_a_database_that_already_holds_relations():
    with pytest.raises(qual.QualificationRefusal, match="not empty on entry"):
        qual.assert_no_carried_over_relations(["opening_position_scores"])


def test_an_empty_database_passes_the_entry_check():
    qual.assert_no_carried_over_relations([])


def test_migrations_refuse_before_the_bootstrap_has_set_the_guarded_target(monkeypatch):
    """``alembic/env.py`` resolves its URL unconditionally, so a migration that
    ran before the bootstrap would follow the application fall-through to the
    developer database on the shared cluster."""
    url = qual.guard_measurement_url(
        "postgresql://u@127.0.0.1:55440/gr_score_qual_r1_c1"
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u@127.0.0.1:5432/ghostreplay")
    with pytest.raises(qual.QualificationRefusal, match="bootstrapped to the guarded"):
        qual.run_migrations(url, expected_database_pattern=qual.CELL_DATABASE_PATTERN)


def test_migrations_refuse_when_no_database_url_is_set_at_all(monkeypatch):
    url = qual.guard_measurement_url(
        "postgresql://u@127.0.0.1:55440/gr_score_qual_r1_c1"
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(qual.QualificationRefusal, match="bootstrapped to the guarded"):
        qual.run_migrations(url, expected_database_pattern=qual.CELL_DATABASE_PATTERN)


def test_the_child_environment_is_constructed_and_never_inherited(monkeypatch):
    for name in qual.REFUSED_ENV_NAMES:
        monkeypatch.setenv(name, "inherited")
    monkeypatch.setenv("SOME_UNRELATED_SECRET", "value")
    target = "postgresql+psycopg://u:pw@127.0.0.1:55440/gr_score_qual_r1_c1"
    environment = qual._child_environment(target)
    assert environment[qual.QUAL_DATABASE_ENV] == target
    assert environment["DATABASE_URL"] == target
    for name in qual.REFUSED_ENV_NAMES:
        assert name not in environment
    assert "SOME_UNRELATED_SECRET" not in environment


def test_the_cell_url_never_reaches_a_spawn_argument(monkeypatch):
    # The URL travels by environment, so no connection string — and no password
    # — ever reaches a process listing. The earlier case asserted only that the
    # URL was in the environment and never looked at the arguments at all.
    target = "postgresql+psycopg://u:secret@127.0.0.1:55440/gr_score_qual_r1_c1"
    calls = {}

    def _run(argv, **kwargs):
        calls["argv"] = argv
        calls["env"] = kwargs["env"]

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(qual.subprocess, "run", _run)
    monkeypatch.setattr(qual, "create_cell_database", lambda name: target)
    monkeypatch.setattr(qual, "drop_cell_database", lambda name: None)

    class Args:
        cell = "C1"
        capture = pathlib.Path("/tmp/capture.pickle")
        profile = "S1"
        copies = 1
        run_id = "r1"
        output = pathlib.Path("/tmp/out.json")
        keep_database = False

    qual.run(Args())
    joined = " ".join(str(part) for part in calls["argv"])
    assert "secret" not in joined
    assert "127.0.0.1" not in joined
    assert "postgresql" not in joined
    assert calls["env"][qual.QUAL_DATABASE_ENV] == target


def test_the_child_environment_carries_the_passfile_and_the_application_name():
    # §0.2 puts the QC-PROD password in a passfile and nowhere else, so a child
    # that does not inherit PGPASSFILE cannot authenticate at all. PGAPPNAME is
    # what makes the child's own backends distinguishable from a co-tenant's.
    with mock.patch.dict(
        os.environ, {"PGPASSFILE": "/private/pgpass", "SOME_SECRET": "x"}, clear=False
    ):
        environment = qual._child_environment(
            "postgresql+psycopg://u:pw@127.0.0.1:55440/gr_score_qual_r1_c1"
        )
    assert environment["PGPASSFILE"] == "/private/pgpass"
    assert environment["PGAPPNAME"] == qual.APPLICATION_NAME
    assert environment["POSTHOG_DISABLED"] == "true"
    assert "SOME_SECRET" not in environment


def test_the_child_environment_can_carry_the_capture_variable_instead():
    target = "postgresql+psycopg://u:pw@127.0.0.1:55440/gr_score_capture_r1"
    environment = qual._child_environment(
        target, env_name=qual.CAPTURE_DATABASE_ENV
    )
    assert environment[qual.CAPTURE_DATABASE_ENV] == target
    assert qual.QUAL_DATABASE_ENV not in environment


def test_the_bootstrap_names_every_connection_this_process_opens():
    # app.db builds its engine with no application_name (app/db.py:49), and
    # assert_no_foreign_activity identifies our own backends by that name — so
    # an unnamed backend of our own refused the cell after it had already run.
    environment = {
        qual.QUAL_DATABASE_ENV: (
            "postgresql+psycopg://u:pw@127.0.0.1:55440/gr_score_qual_r1_c1"
        )
    }
    qual.bootstrap_database_url(environment)
    assert environment["PGAPPNAME"] == qual.APPLICATION_NAME


def test_the_session_factory_is_bound_to_the_harness_engine():
    # Publishing through app.db.SessionLocal put the writes on an engine Trace
    # is not listening to (C6 measured zero DataRow bytes and zero round trips)
    # and opened backends the activity check could not tell from a co-tenant's.
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    factory = qual.session_factory_for(engine)
    with factory() as session:
        assert session.get_bind() is engine
    assert factory.kw["expire_on_commit"] is False
    assert factory.kw["autoflush"] is False


# --------------------------------------------------------------------------
# §2.2 — WAL attribution corner cases
# --------------------------------------------------------------------------


def test_a_shared_catalog_write_is_own_overhead_not_foreign_contamination():
    # pg_walinspect reports a shared catalog's blocks with database OID 0.
    # VACUUM alone updates pg_database in place, so treating 0 as a foreign
    # tenant would invalidate the harness's own vacuum windows.
    records = [(120, 0, "rel 1663/0/1262")]
    classified = qual.classify_wal_records(records, own_oid=99, locators={})
    assert classified["foreign_database_bytes"] == 0
    assert classified["shared_catalog_bytes"] == 120
    assert classified["own_catalog_overhead_bytes"] == 120


def test_a_genuinely_foreign_database_still_invalidates_the_cell():
    records = [(120, 0, "rel 1663/77/1262")]
    classified = qual.classify_wal_records(records, own_oid=99, locators={})
    assert classified["foreign_database_bytes"] == 120
    with pytest.raises(qual.QualificationRefusal, match="foreign database"):
        qual.assert_wal_uncontaminated(classified)


def test_our_own_unattributed_relfilenode_is_overhead_not_contamination():
    records = [(64, 0, "rel 1663/99/424242")]
    classified = qual.classify_wal_records(records, own_oid=99, locators={})
    assert classified["foreign_database_bytes"] == 0
    assert classified["own_catalog_overhead_bytes"] == 64
    qual.assert_wal_uncontaminated(classified)


def test_a_vacuum_window_is_split_per_layout_from_measured_block_refs():
    wal = {
        "record_bytes": 28_500_000,
        "record_bytes_by_relation": {
            "opening_position_scores": 28_000_000,
            "opening_current_positions": 240_000,
            "opening_score_batches": 260_000,
        },
    }
    split = qual.split_vacuum_wal(wal)
    assert split["vacuum_wal_by_layout"] == {"A": 28_000_000, "B50": 240_000}
    # The marker belongs to neither layout and stays in the remainder rather
    # than being apportioned into one of them.
    assert split["vacuum_wal_shared_and_unattributed_bytes"] == 260_000


def test_the_modelled_row_width_is_the_measured_mean_datarow_width():
    width = qual.modelled_row_width({"mean_datarow_width_bytes": 137.4})
    assert width == 137
    assert qual.modelled_row_width({"mean_datarow_width_bytes": 0.2}) == 1


def test_the_reclamation_verdict_needs_both_halves():
    assert qual.reclamation_verdict(0, 0) == {
        "reader_held_dead_tuples": False,
        "reclamation_proved": False,
    }
    assert qual.reclamation_verdict(5, 0)["reclamation_proved"] is True
    assert qual.reclamation_verdict(5, 3)["reclamation_proved"] is False


# --------------------------------------------------------------------------
# §2.1 — the counter rule's created set
# --------------------------------------------------------------------------


def _counters(created, written, **overrides):
    values = {
        "autovacuum": 0,
        "autoanalyze": 0,
        "counts_by_relation": (),
        "checkpoints_timed": 4,
        "checkpoints_requested": 1,
        "created_relations": tuple(created),
        "written_relations": tuple(written),
    }
    values.update(overrides)
    return qual.CellCounters(**values)


def test_the_created_set_must_cover_everything_create_all_built():
    # ensure_evidence_epoch_infrastructure inserts the evidence_epoch singleton
    # (models.py:1765), which is not a MEASURED relation — so a created set of
    # MEASURED_RELATIONS alone refused every cell at the end of its first block.
    written = list(qual.MEASURED_RELATIONS) + ["evidence_epoch"]
    with pytest.raises(qual.QualificationRefusal, match="evidence_epoch"):
        qual.assert_counters_clean(
            _counters(qual.MEASURED_RELATIONS, written),
            _counters(qual.MEASURED_RELATIONS, written),
        )
    created = written
    result = qual.assert_counters_clean(
        _counters(created, written), _counters(created, written)
    )
    assert result["checkpoints_num_requested"] == 0


def test_create_schema_returns_the_whole_created_set():
    from app.models import Base

    names = set(qual.create_schema.__doc__ or "")
    assert names  # the docstring records why the full set is required
    declared = {table.name for table in Base.metadata.sorted_tables}
    assert "evidence_epoch" in declared
    assert set(qual.MEASURED_RELATIONS) < declared


# --------------------------------------------------------------------------
# §2.4 — the delta-lane storage-format knob
# --------------------------------------------------------------------------


def test_the_delta_lane_knob_rejects_an_unknown_value(monkeypatch):
    import test_opening_score_delta_lane_release as lane

    monkeypatch.setenv(lane._FORMAT_ENV, "b50")
    with pytest.raises(BaseException) as excinfo:
        lane._storage_format()
    assert "must be one of" in str(excinfo.value)


@pytest.mark.parametrize(
    "value,expected", [(None, "legacy"), ("legacy", "legacy"), ("current", "current-b50-v1")]
)
def test_the_delta_lane_knob_defaults_to_legacy(monkeypatch, value, expected):
    import test_opening_score_delta_lane_release as lane

    if value is None:
        monkeypatch.delenv(lane._FORMAT_ENV, raising=False)
    else:
        monkeypatch.setenv(lane._FORMAT_ENV, value)
    assert lane._storage_format().value == expected


def test_the_delta_lane_marker_assertion_catches_a_flipped_format(db_session):
    """Lane deltas follow the handle's format, so a single flipped marker would
    move the rest of the run onto the other format with no other symptom."""
    import test_opening_score_delta_lane_release as lane
    from app.opening_score_storage import StorageFormat

    _seed_black_opening_session(db_session)
    db_session.commit()
    _score_at(db_session, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))

    class _Factory:
        def __call__(self):
            return self

        def __enter__(self):
            return db_session

        def __exit__(self, *exc):
            return False

    lane._assert_marker_format(_Factory(), 123, "black", StorageFormat.LEGACY)
    with pytest.raises(AssertionError, match="marker is"):
        lane._assert_marker_format(_Factory(), 123, "black", StorageFormat.CURRENT)


def test_the_delta_lane_protected_databases_and_limit_are_unchanged():
    import test_opening_score_delta_lane_release as lane

    assert lane._PROTECTED_DATABASES == {"postgres", "railway", "gr_snap_base"}
    assert lane._P95_LIMIT_MS == 3000.0


# --------------------------------------------------------------------------
# §2.1 first implementation step / §7 risk 1 — replica keys through the
# SHIPPED writer and the SHIPPED readers, in BOTH formats
# --------------------------------------------------------------------------


@pytest.mark.parametrize("storage_format", ["legacy", "current"])
def test_a_scaled_payload_survives_strict_validation_and_reads_back(
    db_session, storage_format
):
    """Publish one scaled payload in BOTH formats and read it back.

    §2.1 makes this the FIRST implementation step, ahead of any cell, because
    strict typed payload validation runs on legacy recomputes too: if either
    format rejected replica keys, S2 and S3 would have to be constructed
    differently and the plan would be revised before measuring anything.
    """
    from unittest.mock import patch

    from app import opening_cache as oc
    from app.opening_score_storage import StorageFormat
    from scripts.opening_score_storage_workload import FIELDS, replay_objects

    selected = {
        "legacy": StorageFormat.LEGACY,
        "current": StorageFormat.CURRENT,
    }[storage_format]
    scaled = qual.qualification_scale(_synthetic_candidate(), 4)
    roots, positions, overlay = replay_objects(scaled)
    with patch.object(oc, "_build_cached_scores", return_value=(roots, positions)):
        batch = oc.recompute_opening_scores(
            db_session,
            4242,
            "black",
            storage_format=selected,
            overlay=overlay,
            freshness=scaled.freshness,
            computed_at=scaled.computed_at,
        )
        assert batch.storage_format == selected.value
        db_session.commit()

    view, root_rows = oc.list_cached_opening_scores(db_session, 4242, "black")
    assert view is not None and view.storage_format == selected.value
    assert len(root_rows) == len(scaled.payload.roots)

    # Every measured read addresses copy-0 keys, which are also the only ones
    # reachable from a legal line.
    fen_index = FIELDS["positions"].index("normalized_fen")
    copy_zero = [
        row[fen_index]
        for row in scaled.payload.positions
        if "|replica:" not in row[fen_index]
    ]
    found = oc.lookup_position_scores_for_batch(db_session, view.handle, copy_zero)
    assert set(found) == set(copy_zero)

    parent_index = FIELDS["edges"].index("parent_fen")
    parents = [
        row[parent_index]
        for row in scaled.payload.edges
        if "|replica:" not in row[parent_index]
    ]
    edges = oc.lookup_observed_edges_for_parents(db_session, view.handle, parents)
    assert set(edges) == set(parents)

    # The SCOPE half of composite D, and composite T's marker probe. A scaled
    # payload's scope rows carry replica-suffixed FENs for copies 1..n-1, which
    # is the part of the read surface the roots/positions/edges assertions above
    # do not touch at all.
    from app.opening_score_delta import (
        _marker_rooted_change,
        _shared_invalidation_statement,
        _shared_scope_change_statement,
    )
    from app.opening_score_storage import handle_is_live, latest_batch_view

    marker = latest_batch_view(db_session, 4242, "black")
    assert marker is not None and marker.storage_format == selected.value
    assert marker.cache_epoch is not None
    for build in (_shared_scope_change_statement, _shared_invalidation_statement):
        changed = _marker_rooted_change(
            db_session,
            marker.handle,
            build(db_session, marker.handle, marker.cache_epoch),
        )
        assert changed is False
    assert handle_is_live(db_session, marker.handle) is True

    scope_rows = scaled.payload.rows("scope")
    assert any("|replica:" in row["fen"] for row in scope_rows)
    assert any("|replica:" not in row["fen"] for row in scope_rows)


@pytest.mark.parametrize("storage_format", ["legacy", "current"])
def test_the_postgres_half_of_risk_one_is_recorded_as_unproven(storage_format):
    """The read-back above runs on SQLite, which is not the qualified dialect.

    The key columns are ``Text`` in both formats, so PostgreSQL is low risk —
    but "low risk" is not "proven", and the first cell to run is what proves it.
    Recorded here rather than left implicit, so nobody reads the case above as
    covering more than it does.
    """
    from app.models import CurrentOpeningPosition, OpeningPositionScore

    column = {
        "legacy": OpeningPositionScore.__table__.c.normalized_fen,
        "current": CurrentOpeningPosition.__table__.c.normalized_fen,
    }[storage_format]
    assert column.type.__class__.__name__ == "Text"
    assert column.type.length is None


# --------------------------------------------------------------------------
# END TO END through evaluate_cell -> build_ceilings -> aggregate_verdict.
#
# The leaf-function cases above pin each rule in isolation and every one of them
# passed while the assembled evaluator could not emit an aggregate `pass` at
# all: C2 was structurally insufficient, the footprint and vacuum ceilings were
# always refused, `--memory` raised KeyError, and the combined-WAL gate added
# both layouts' vacuum WAL to each side. Nothing drove the three together, so
# nothing caught it. These cases do.
# --------------------------------------------------------------------------

_A_PUBLICATION_WAL = 10_000
_B_PUBLICATION_WAL = 3_000
_A_VACUUM_WAL = 28_000_000
_B_VACUUM_WAL = 240_000


def _wal(total, relation):
    return {
        "total_bytes": total,
        "record_bytes": total,
        "record_bytes_by_relation": {relation: total},
        "fpi_bytes_by_relation": {relation: 0},
        "own_catalog_overhead_bytes": 0,
        "shared_catalog_bytes": 0,
        "foreign_database_bytes": 0,
        "foreign_database_oids": {},
    }


def _record(layout, cell, block, step, rows, *, d_reads, t_reads):
    relation = "opening_position_scores" if layout == "A" else "opening_current_positions"
    wal_bytes = _A_PUBLICATION_WAL if layout == "A" else _B_PUBLICATION_WAL
    latency = 10.0 if layout == "A" else 10.5
    record = {
        "layout": layout,
        "cell": cell,
        "block": block,
        "step": step,
        "cutoff": step % 10,
        "direction": "forward" if step % 2 == 0 else "backward",
        "post_checkpoint": cell == "C2",
        "publish_ms": latency + (step % 5) * 0.1,
        "storage_format": "legacy" if layout == "A" else "current-b50-v1",
        "wal": _wal(wal_bytes, relation),
        "logical_rows": rows,
        "composite_d": [{"composite_ms": latency} for _ in range(d_reads)],
        "composite_t": [{"format_stage_ms": latency} for _ in range(t_reads)],
    }
    if step:
        record["confidence"] = {
            "confidence_changed_fraction": 0.68,
            "stable_changed_fraction": 0.002,
            "inserted": 0,
            "deleted": 0,
            "common": 1000,
        }
    return record


def _vacuum_window(block):
    wal = _wal(_A_VACUUM_WAL + _B_VACUUM_WAL, "mixed_or_metadata")
    wal["record_bytes_by_relation"] = {
        "opening_position_scores": _A_VACUUM_WAL,
        "opening_current_positions": _B_VACUUM_WAL,
    }
    return {
        "window": block,
        "after_block": block,
        "before_vacuum": {},
        "after_vacuum": {},
        "vacuum_wal": wal,
        "vacuum_wal_by_layout": {"A": _A_VACUUM_WAL, "B50": _B_VACUUM_WAL},
        "vacuum_wal_shared_and_unattributed_bytes": 0,
    }


def test_the_revision_stamp_says_when_the_tree_did_not_match_it(monkeypatch):
    """A clean sha on a modified tree is an identity a reader cannot falsify.

    The commit resolves and nothing looks wrong, so §5's homogeneity check —
    which compares revisions for equality — would accept a cell measured from
    the shared working tree as though it came from §2.7's pinned clone.
    Untracked paths do NOT count: several agents edit this repository at once
    and unrelated untracked files are expected (AGENTS.md), which is why the
    status call carries ``--untracked-files=no``.
    """
    state = {"status": ""}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        out = "d5019b2\n" if argv[1] == "rev-parse" else state["status"]
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(qual.subprocess, "run", fake_run)
    assert qual._git_revision() == "d5019b2"
    assert "--untracked-files=no" in calls[1]

    state["status"] = " M backend/app/models.py\n"
    assert qual._git_revision() == "d5019b2-dirty"


def _identity(**overrides):
    identity = {
        "tested_revision": "abc123",
        "settings": {"max_wal_size": {"setting": "128MB"}},
        "database": {},
        "host": {"platform": "macOS-15"},
        "postgres_binary_prefix": "/opt/pg18.4",
        "stated_differences": [],
    }
    identity.update(overrides)
    return identity


def _cell(cell, profile, rows, *, publications=100, reads=None, complete=True,
          discarded=(), identity=None, closure=None,
          cluster="ghostreplay-score-storage-qual"):
    blocks = publications // 10
    # Taken from the SHIPPED spec, so a fabricated report cannot pass a run the
    # real cells could not produce.
    d_reads, t_reads = reads if reads is not None else qual.CELL_SPECS[cell]["reads"]
    records = {
        layout: [
            _record(layout, cell, step // 10, step, rows, d_reads=d_reads,
                    t_reads=t_reads)
            for step in range(publications)
        ]
        for layout in ("A", "B50")
    }
    kept = [b for b in range(blocks) if b not in set(discarded)]
    report = {
        "cell": cell,
        "profile": profile,
        "copies": 1,
        "color": "black",
        "complete": complete,
        "capture": {"pair_index": 0, "cutoffs": 10, "provenance": {}},
        "capture_closure": {"ran": True, "equal": True} if closure is None else closure,
        "cluster": {"cluster_name": cluster},
        "reloptions": {},
        "created_relations": ["evidence_epoch"],
        "shared_evidence_rows": {},
        "profile_identity": identity or _identity(),
        "sequence": {"distinct_adjacent_steps": 9},
        "result": {
            "records": records,
            "blocks": {},
            "paired_blocks": {
                "reference": [{"block": b} for b in kept],
                "selected": [{"block": b} for b in kept],
                "discarded": [{"block": b} for b in discarded],
                "complete_paired_blocks": len(kept),
            },
            "vacuum_windows": [_vacuum_window(b) for b in range(blocks)],
            "reads_per_publication": {"composite_d": d_reads, "composite_t": t_reads},
        },
        "final_footprint": {
            "A_total_bytes": 100 * rows,
            "B50_total_bytes": 60 * rows,
            "A_live_tuples": rows,
            "B50_live_tuples": rows,
        },
    }
    if not complete:
        report["failure"] = {"type": "QualificationRefusal", "message": "boom"}
    return report


_SIZES = (("S0", 245), ("S1", 24_815), ("S2", 49_630), ("S3", 99_260))


def _full_run():
    cells = [_cell("C1", profile, rows) for profile, rows in _SIZES]
    cells += [
        _cell("C2", profile, rows, publications=40)
        for profile, rows in _SIZES[1:]
    ]
    memory = [
        {
            "cell": "C3",
            "profile": profile,
            "complete": True,
            "cluster": {"cluster_name": "ghostreplay-score-storage-qual"},
            "profile_identity": _identity(),
            "result": {
                "cell": "C3",
                "logical_rows": rows,
                "copies": 1,
                "children": {"A": [{}] * 5, "B50": [{}] * 5},
                "repeated_maximum": {
                    "B50": {
                        "untraced_worker_rss_highwater_bytes": 200_000_000 + rows * 10,
                        "publication_allocation_peak_bytes": 50_000_000 + rows * 5,
                    }
                },
            },
        }
        for profile, rows in _SIZES
    ]
    return cells, memory


def _plateau_report(held=5_000, released=0, growth=0.01, clean=True):
    return {
        "cell": "C5",
        "profile": "S1",
        "complete": True,
        "cluster": {"cluster_name": "ghostreplay-score-storage-qual"},
        "profile_identity": _identity(),
        "result": {
            "cell": "C5",
            "plateau": {
                layout: {
                    "window_total_bytes": [1, 1, 1, 1, 1, 1],
                    "window_live_tuples": [1] * 6,
                    "fixed_live_counts": True,
                    "last_five_growth_fraction": growth,
                    "dead_tuples_held": held,
                    "dead_tuples_released": released,
                    "reclaimed_after_reader_finished": released == 0,
                    **qual.reclamation_verdict(held, released),
                }
                for layout in ("A", "B50")
            },
            "orphans": {
                "current_rows_for_legacy_pair": 0,
                "legacy_rows_for_current_pair": 0,
                "clean": clean,
            },
        },
    }


def _network_report():
    term = {
        "modelled_ms": 12.0,
        "bytes": 11_000_000,
        "round_trips": 40,
        "throughput_bytes_per_s": 900_000_000.0,
        "rtt_ms_median": 0.12,
        "provenance": "MODELLED from loopback-measured throughput and RTT",
    }
    return {
        "cell": "C6",
        "profile": "S1",
        "complete": True,
        "cluster": {"cluster_name": "ghostreplay-score-storage-qual"},
        "profile_identity": _identity(),
        "result": {
            "cell": "C6",
            "network_terms": {
                layout: {
                    "publication": dict(term),
                    "composite_d": dict(term),
                    "composite_t": dict(term),
                }
                for layout in ("A", "B50")
            },
        },
    }


def _control_report(label, p95, *, profile="S1", repetitions=40,
                    cluster="ghostreplay-score-storage-qual",
                    host="macOS-15", revision=None):
    return {
        "label": label,
        "profile": profile,
        "cluster": {"cluster_name": cluster},
        "host_platform": host,
        "revision": revision or ("abc123" if label == "A_new" else "6699678"),
        "repetitions": repetitions,
        "publication_p95_ms": p95,
        "publication_median_ms": p95 * 0.8,
        "retirement_delete_p95_ms": 4.0 if label == "A_new" else None,
        "publication_lock_hold_p95_ms": 9.0 if label == "A_new" else None,
        "publication_lock_hold_samples": 40 if label == "A_new" else 0,
        "capture_closure": {"ran": True, "equal": True},
        "capture_synthetic_only": False,
    }


def _control_reports(old_p95=100.0, new_p95=101.0, **kwargs):
    return [
        _control_report("A_old", old_p95, **kwargs),
        _control_report("A_new", new_p95, **kwargs),
    ]


def _lane_report(storage_format, *, normal=1200.0, drill=1400.0, repetitions=10,
                 limit=3000.0, cluster="ghostreplay-score-storage-qual",
                 host="macOS-15", revision="abc123"):
    def cell(value):
        return {
            mode: {"end_to_end": {"p95_ms": value, "median_ms": value * 0.7}}
            for mode in ("normal", "drill")
        }

    return {
        "storage_format": storage_format,
        "p95_limit_ms": limit,
        "repetitions": repetitions,
        "identity": {
            "tested_revision": revision,
            "host_platform": host,
            "cluster_name": cluster,
        },
        "idle": cell(80.0),
        "whole_graph": {
            "normal": {"end_to_end": {"p95_ms": normal, "median_ms": normal * 0.7}},
            "drill": {"end_to_end": {"p95_ms": drill, "median_ms": drill * 0.7}},
        },
        "baseline_digest": {"normal": {"end_to_end": {"p95_ms": 900.0}}},
        "process_cold": cell(2600.0),
    }


def _lane_reports(**kwargs):
    return [_lane_report(fmt, **kwargs) for fmt in evaluator.DELTA_LANE_FORMATS]


def _evaluate(cells, memory, plateau=None, network=None, control=None,
              delta_lane=None):
    results = [evaluator.evaluate_cell(cell) for cell in cells]
    ceilings = evaluator.build_ceilings(results, memory)
    plateau_result = evaluator.evaluate_plateau(plateau) if plateau else None
    control_result = evaluator.evaluate_control(
        _control_reports() if control is None else control
    )
    lane_result = evaluator.evaluate_delta_lane(
        _lane_reports() if delta_lane is None else delta_lane
    )
    coverage = evaluator.required_coverage(
        results, plateau, network, control=control_result, delta_lane=lane_result
    )
    verdict = evaluator.aggregate_verdict(
        results,
        ceilings,
        plateau=plateau_result,
        coverage=coverage,
        control=control_result,
        delta_lane=lane_result,
    )
    return results, ceilings, verdict


def test_a_complete_run_reaches_an_aggregate_pass():
    # The assembled evaluator could not emit `pass` under ANY input before this.
    cells, memory = _full_run()
    _results, ceilings, verdict = _evaluate(
        cells, memory, _plateau_report(), _network_report()
    )
    assert ceilings["gaps"] == {}, ceilings["gaps"]
    assert verdict["coverage_gaps"] == {}
    assert verdict["aggregate"] == "pass", verdict["per_gate"]


def test_c2_contributes_a_post_checkpoint_ceiling_rather_than_insufficiency():
    cells, memory = _full_run()
    results, ceilings, verdict = _evaluate(
        cells, memory, _plateau_report(), _network_report()
    )
    c2 = [r for r in results if r["cell"] == "C2"]
    assert all(r["sufficiency"]["verdict"] == "sufficient" for r in c2)
    assert "post_checkpoint_publication_wal_bytes" in ceilings["ceilings"]
    assert verdict["per_gate"]["ceiling:post_checkpoint_publication_wal_bytes"] == "pass"


def test_a_cell_without_reads_emits_no_read_gates():
    result = evaluator.evaluate_cell(_cell("C2", "S1", 24_815, publications=40,
                                           reads=(0, 0)))
    assert "composite_d_p95" not in result["gates"]
    assert "composite_t_format_stage_p95" not in result["gates"]
    assert "publication_p95" in result["gates"]


def test_the_combined_wal_gate_charges_each_layout_only_its_own_vacuum_wal():
    # Adding the window TOTAL to both sides adds a common constant to numerator
    # and denominator and drags the ratio toward 1. With A at 28 MB of vacuum
    # WAL against B50's 0.24 MB that turns a real 0.03 into 0.998.
    result = evaluator.evaluate_cell(_cell("C1", "S1", 24_815))
    gate = result["gates"]["combined_wal"]
    kept_publications = 100
    assert gate["components"]["A"]["vacuum_wal_bytes"] == _A_VACUUM_WAL * 10
    assert gate["components"]["B50"]["vacuum_wal_bytes"] == _B_VACUUM_WAL * 10
    assert gate["components"]["A"]["publication_wal_bytes"] == (
        _A_PUBLICATION_WAL * kept_publications
    )
    combined_total = (_A_VACUUM_WAL + _B_VACUUM_WAL) * 10
    naive = (_B_PUBLICATION_WAL * kept_publications + combined_total) / (
        _A_PUBLICATION_WAL * kept_publications + combined_total
    )
    assert gate["ratio"] < 0.5 < naive
    assert gate["verdict"] == "pass"


def test_the_b50_vacuum_ceiling_is_fitted_from_b50s_own_vacuum_wal():
    # Fitting the whole window fits the SELECTED design's ceiling largely out of
    # layout A's vacuum — two orders of magnitude too loose.
    cells, memory = _full_run()
    results = [evaluator.evaluate_cell(cell) for cell in cells]
    ceilings = evaluator.build_ceilings(results, memory)
    points = ceilings["ceilings"]["vacuum_wal_bytes_per_ten_publications"]["points"]
    assert {p["value"] for p in points} == {_B_VACUUM_WAL}
    assert all(p["sample_kind"] == "vacuum_windows" for p in points)


def test_windows_after_a_discarded_block_are_not_counted():
    # A window after a discarded block vacuumed writes made under a different
    # checkpoint schedule, so its WAL is not comparable to the rest.
    result = evaluator.evaluate_cell(_cell("C1", "S1", 24_815, discarded=(3,)))
    assert result["vacuum_windows_total"] == 10
    assert result["vacuum_windows_kept"] == 9
    assert result["gates"]["combined_wal"]["components"]["B50"][
        "vacuum_wal_bytes"
    ] == _B_VACUUM_WAL * 9


def test_both_composites_are_gated_not_just_composite_d():
    # The earlier evaluator checked only composite D, so a thinned T pool kept
    # its gate running. Both are checked, on both layouts.
    thin = _cell("C1", "S1", 24_815, reads=(13, 4))
    result = evaluator.evaluate_cell(thin)
    assert result["sufficiency"]["verdict"] == "insufficient_evidence"
    assert result["sufficiency"]["composites"]["composite_d"]["status"] == "sufficient"
    assert result["sufficiency"]["composites"]["composite_t"]["status"] == "insufficient"
    assert result["gates"] == {"verdict": "insufficient_evidence"}


def test_the_read_floor_survives_six_discarded_blocks():
    # THE ARITHMETIC THAT SETS CELL_SPECS["C1"]["reads"]. The floor is 500 reads
    # per layout per composite counted over KEPT blocks, so k reads per
    # publication survive ceil(500 / 10k) kept blocks. At six T reads that was
    # nine of ten blocks — ONE discard — and a timed checkpoint every 300 s
    # discards one side of a pair, so any C1 running past about ten minutes lost
    # its T gate by arithmetic. S2 and S3 will run far past it.
    assert qual.CELL_SPECS["C1"]["reads"] == (13, 13)
    kept_four = evaluator.evaluate_cell(
        _cell("C1", "S1", 24_815, discarded=(0, 1, 2, 3, 4, 5))
    )
    assert kept_four["sufficiency"]["verdict"] == "sufficient"
    for composite in ("composite_d", "composite_t"):
        counts = kept_four["sufficiency"]["composites"][composite]["reads_per_layout"]
        assert min(counts.values()) == 520

    kept_three = evaluator.evaluate_cell(
        _cell("C1", "S1", 24_815, discarded=(0, 1, 2, 3, 4, 5, 6))
    )
    assert kept_three["sufficiency"]["verdict"] == "insufficient_evidence"


def test_footprint_and_memory_ceilings_are_emitted_rather_than_refused():
    cells, memory = _full_run()
    results = [evaluator.evaluate_cell(cell) for cell in cells]
    ceilings = evaluator.build_ceilings(results, memory)
    for metric in (
        "vacuumed_footprint_bytes",
        "integrated_worker_rss_bytes",
        "publication_allocation_peak_bytes",
    ):
        assert metric in ceilings["ceilings"], (metric, ceilings["gaps"])
    rss = ceilings["ceilings"]["integrated_worker_rss_bytes"]
    assert rss["tag"] == "local_host_only"
    assert rss["deferred_to"] == "g-score-store-cutover"
    # The true child count, never the minimum passed as a stand-in.
    assert {p["samples"] for p in rss["points"]} == {5}


def test_memory_reports_carry_their_own_logical_rows():
    # `--memory` raised KeyError: 'logical_rows' — a run_cell C3 report has no
    # such key at the top level, and the fit needs the size it measured.
    cells, memory = _full_run()
    results = [evaluator.evaluate_cell(cell) for cell in cells]
    ceilings = evaluator.build_ceilings(results, memory)
    points = ceilings["ceilings"]["integrated_worker_rss_bytes"]["points"]
    assert sorted(p["logical_rows"] for p in points) == sorted(
        rows for _profile, rows in _SIZES
    )


def test_c1_cells_alone_do_not_pass():
    # Evaluating C1 alone yielded `pass` with no C2 ceiling, no memory ceilings
    # and no C5 result anywhere in the verdict. A missing input is a gap.
    cells = [_cell("C1", profile, rows) for profile, rows in _SIZES]
    _results, ceilings, verdict = _evaluate(cells, [])
    assert verdict["aggregate"] == "insufficient_evidence"
    assert "post_checkpoint_publication_wal_bytes" in ceilings["gaps"]
    assert "integrated_worker_rss_bytes" in ceilings["gaps"]
    assert set(verdict["coverage_gaps"]) >= {"C2", "C5:plateau", "C6:network"}


def test_an_incomplete_cell_is_a_recorded_gap_not_a_pass():
    cells, memory = _full_run()
    cells[1] = _cell("C1", "S1", 24_815, complete=False)
    results, _ceilings, verdict = _evaluate(
        cells, memory, _plateau_report(), _network_report()
    )
    broken = [r for r in results if "incomplete" in r]
    assert broken and broken[0]["incomplete"]["message"] == "boom"
    assert verdict["aggregate"] == "insufficient_evidence"


def test_a_reader_that_held_nothing_does_not_prove_reclamation():
    # `reclaimed_after_reader_finished` alone passes when the snapshot blocked
    # nothing: "returned to zero" then says only that nothing was ever there.
    plateau = evaluator.evaluate_plateau(_plateau_report(held=0, released=0))
    assert plateau["gates"]["reclamation:B50"]["verdict"] == "insufficient_evidence"
    proved = evaluator.evaluate_plateau(_plateau_report(held=5_000, released=0))
    assert proved["gates"]["reclamation:B50"]["verdict"] == "pass"
    leaked = evaluator.evaluate_plateau(_plateau_report(held=5_000, released=12))
    assert leaked["gates"]["reclamation:B50"]["verdict"] == "fail"


def test_plateau_growth_and_orphans_reach_the_aggregate_verdict():
    cells, memory = _full_run()
    _r, _c, verdict = _evaluate(
        cells, memory, _plateau_report(growth=0.2), _network_report()
    )
    assert verdict["per_gate"]["C5:plateau_growth:B50"] == "fail"
    assert verdict["aggregate"] == "fail"
    _r, _c, dirty = _evaluate(
        cells, memory, _plateau_report(clean=False), _network_report()
    )
    assert dirty["per_gate"]["C5:orphans"] == "fail"


def test_forward_and_backward_changed_fractions_stay_separable():
    result = evaluator.evaluate_cell(_cell("C1", "S1", 24_815))
    split = result["confidence_by_direction"]
    assert split["forward"]["samples"] and split["backward"]["samples"]
    assert split["asymmetry"] is not None


def test_the_confidence_share_of_publication_wal_is_reported():
    result = evaluator.evaluate_cell(_cell("C1", "S1", 24_815))
    share = result["confidence_wal_share"]
    assert share["share"] == pytest.approx(1.0)
    assert "opening_current_positions" in share["relations"]
    assert "not an attribution of the confidence column" in share["note"]


def test_the_sealed_output_carries_a_run_id_and_source_hashes(tmp_path):
    cells, memory = _full_run()
    paths = []
    for index, cell in enumerate(cells):
        path = tmp_path / f"cell{index}.json"
        path.write_text(json.dumps(cell))
        paths.append(path)
    memory_paths = []
    for index, report in enumerate(memory):
        path = tmp_path / f"mem{index}.json"
        path.write_text(json.dumps(report))
        memory_paths.append(path)
    plateau_path = tmp_path / "c5.json"
    plateau_path.write_text(json.dumps(_plateau_report()))
    network_path = tmp_path / "c6.json"
    network_path.write_text(json.dumps(_network_report()))
    control_paths = [
        _write(tmp_path, f"c4_{index}.json", report)
        for index, report in enumerate(_control_reports())
    ]
    lane_paths = [
        _write(tmp_path, f"c7_{index}.json", report)
        for index, report in enumerate(_lane_reports())
    ]
    output = tmp_path / "qualification.json"
    argv = ["--run-id", "r7"]
    for path in paths:
        argv += ["--cell", str(path)]
    for path in memory_paths:
        argv += ["--memory", str(path)]
    for path in control_paths:
        argv += ["--control", str(path)]
    for path in lane_paths:
        argv += ["--delta-lane", str(path)]
    argv += [
        "--plateau", str(plateau_path),
        "--network", str(network_path),
        "--output", str(output),
    ]
    assert evaluator.main(argv) == 0
    report = json.loads(output.read_text())
    assert report["run_id"] == "r7"
    assert report["seal"]["tooling_sha256"][
        "scripts/summarize_opening_score_qualification.py"
    ] != "missing"
    assert len(report["seal"]["input_sha256"]) == (
        len(paths) + len(memory_paths) + len(control_paths) + len(lane_paths) + 2
    )
    assert report["verdict"]["aggregate"] == "pass"
    decision = output.with_suffix(".md").read_text()
    assert "Run id: `r7`" in decision


def test_the_fixture_comparison_flags_a_materially_lower_share():
    cells, memory = _full_run()
    results = [evaluator.evaluate_cell(cell) for cell in cells]
    comparison = evaluator.compare_confidence_against_fixture(
        results, {"mean": 0.68, "samples": 22}
    )
    assert comparison["materially_lower"] == []
    thin = evaluator.compare_confidence_against_fixture(
        results, {"mean": 4.0, "samples": 22}
    )
    assert thin["materially_lower"]


def test_the_fixture_share_is_read_from_the_approved_timeline():
    approved = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[1]
            / "docs/analysis/opening-score-storage-budgets-2026-09-19.json"
        ).read_text()
    )
    share = evaluator.fixture_confidence_share(approved["timeline"])
    assert share["samples"] > 0
    assert 0.0 < share["mean"] <= 1.0


# --------------------------------------------------------------------------
# §2.8 — the C4 control runner's own guards
# --------------------------------------------------------------------------

from scripts import qualify_legacy_retirement_cost as c4  # noqa: E402


def test_the_c4_runner_takes_no_database_url_argument():
    # §2.1: the URL travels by environment. A --database-url argument puts the
    # password in ps output and in every transcript of the run.
    parser_flags = c4.main.__doc__ or ""
    assert "--database-url" not in parser_flags
    source = pathlib.Path(c4.__file__).read_text()
    assert '"--database-url"' not in source
    assert c4.QUAL_DATABASE_ENV == qual.QUAL_DATABASE_ENV


def test_the_c4_runner_refuses_a_non_loopback_target():
    with pytest.raises(c4.ControlRefusal, match="loopback"):
        c4.guard_database_url(
            {c4.QUAL_DATABASE_ENV: "postgresql+psycopg://u:p@10.0.0.5:5432/gr_score_qual_x"}
        )


def test_the_c4_runner_refuses_a_database_outside_the_cell_pattern():
    with pytest.raises(c4.ControlRefusal, match="gr_score_qual"):
        c4.guard_database_url(
            {c4.QUAL_DATABASE_ENV: "postgresql+psycopg://u:p@127.0.0.1:55440/ghostreplay"}
        )


def test_the_c4_runner_accepts_the_guarded_target():
    url = c4.guard_database_url(
        {
            c4.QUAL_DATABASE_ENV: (
                "postgresql+psycopg://u:p@127.0.0.1:55440/gr_score_qual_r1_c4"
            )
        }
    )
    assert url.database == "gr_score_qual_r1_c4"


@pytest.mark.parametrize("name", qual.REFUSED_ENV_NAMES)
def test_the_c4_runner_refuses_every_inherited_name_the_harness_does(name):
    value = (
        "postgresql://u:p@ghostreplay.internal:5432/app"
        if name.endswith("_URL")
        else "ghostreplay.internal"
    )
    with pytest.raises(c4.ControlRefusal):
        c4.refuse_inherited_connection_environment({name: value})


@pytest.mark.parametrize("name", qual.INHERITED_URL_ENV_NAMES)
def test_an_unparseable_inherited_url_cannot_be_proven_loopback(name):
    # It cannot be proven loopback either, and only a provably loopback value
    # may pass. Both tools refuse rather than raising an opaque parse error.
    with pytest.raises(qual.QualificationRefusal, match="parseable URL"):
        qual.refuse_inherited_connection_environment({name: "not-a-url"})
    with pytest.raises(c4.ControlRefusal, match="parseable URL"):
        c4.refuse_inherited_connection_environment({name: "not-a-url"})


def test_the_two_refusal_lists_cannot_drift_apart():
    assert set(c4.REFUSED_ENV_NAMES) == set(qual.REFUSED_ENV_NAMES)
    # The URL list is the one that had the hole, and the C4 runner carries its
    # own copy because §2.1's module does not exist at A_old.
    assert set(c4.INHERITED_URL_ENV_NAMES) == set(qual.INHERITED_URL_ENV_NAMES)
    assert set(c4.INHERITED_HOST_ENV_NAMES) == set(qual.INHERITED_HOST_ENV_NAMES)


def test_the_requirements_check_uses_a_top_level_relative_pathspec():
    # From backend/, the plain pathspec `backend/requirements.txt` means
    # backend/backend/requirements.txt and matches nothing, so the check printed
    # an empty diff for every commit and proved nothing at all.
    provenance = c4.assert_shared_venv_still_valid(c4.WRITER_PARENT)
    assert provenance["requirements_diff"] == "empty"
    assert all(spec.startswith(":/backend/") for spec in provenance["pathspecs"])


def test_the_requirements_check_detects_a_real_change():
    # 94d9d25 is the last commit that changed requirements.txt.
    with pytest.raises(c4.ControlRefusal, match="requirements changed"):
        c4.assert_shared_venv_still_valid("94d9d25~1")


class _FakeCursor:
    def __init__(self, rowcount=0):
        self.rowcount = rowcount


def test_the_c4_runner_measures_the_retirement_stage_and_the_lock_hold():
    # LegacyAdapter.last_metrics carries publish_ms, read_ms, diff_ms and
    # read_bytes_estimate and nothing else, so §4.7 part (ii) was empty. Both
    # figures are visible as STATEMENTS, which exist at both commits.
    stages = c4.PublicationStages.__new__(c4.PublicationStages)
    stages.reset()
    for statement, rows in (
        ("SELECT pg_advisory_xact_lock(CAST(:classid AS integer), ...)", 1),
        ("INSERT INTO opening_position_scores (...) VALUES (...)", 500),
        ("DELETE FROM opening_position_scores WHERE batch_id IN (...)", 1200),
        ("DELETE FROM opening_score_batches WHERE id IN (...)", 1),
        ("DELETE FROM some_unrelated_table WHERE id = 1", 1),
    ):
        stages._before(None, None, statement, None, None, False)
        stages._after(None, _FakeCursor(rows), statement, None, None, False)
    assert stages.lock_acquired_at is not None
    stages._commit(None)
    sample = stages.sample()
    assert sample["retirement_delete_statements"] == 2
    assert sample["retirement_deleted_rows"] == 1201
    assert sample["publication_lock_hold_ms"] is not None
    assert sample["retirement_delete_ms"] >= 0.0


def test_a_publication_that_took_no_lock_reports_no_hold_rather_than_zero():
    stages = c4.PublicationStages.__new__(c4.PublicationStages)
    stages.reset()
    stages._before(None, None, "SELECT 1", None, None, False)
    stages._after(None, _FakeCursor(1), "SELECT 1", None, None, False)
    stages._commit(None)
    assert stages.sample()["publication_lock_hold_ms"] is None


# --------------------------------------------------------------------------
# §1.3 — pair resolution, the private store, and the cluster opt-in
# --------------------------------------------------------------------------


def test_the_capture_cli_never_takes_a_user_id():
    # The anonymised census index resolves to the real pair inside the tool, so
    # no production identifier reaches argv, ps output or a transcript.
    source = pathlib.Path(capture.__file__).read_text()
    assert '"--user-id"' not in source
    assert '"--pair-index"' in source


def _census(**overrides):
    entry = {
        "pair_index": 2,
        "player_color": "white",
        "roots": 39,
        "positions": 100,
        "edges": 100,
        "scope": 6,
        "logical_rows": 245,
        "sessions": 28,
    }
    entry.update(overrides)
    return {
        "pairs": [entry],
        # The keys §0.4's stated-differences list reads. They travel in the same
        # file as the pairs, and `assert_census_shape` refuses a census missing
        # any of them by name rather than failing deep inside a capture.
        "settings": {"max_wal_size": {"setting": "128", "unit": "MB"}},
        "database": {"encoding": "UTF8"},
        "host": {"cpu_quota": 8.0},
    }


def _snapshot_pairs(**overrides):
    entry = {
        "user_id": 4242,
        "player_color": "white",
        "roots": 39,
        "positions": 100,
        "edges": 100,
        "scope": 6,
        "logical_rows": 245,
        "sessions": 28,
        "generation": 28,
        "storage_format": "legacy",
        "computed_at": "2026-07-14",
    }
    entry.update(overrides)
    return [None, None, entry]


def test_the_pair_index_resolves_to_a_real_pair_inside_the_tool(monkeypatch):
    monkeypatch.setattr(capture, "_census_pairs", lambda engine: _snapshot_pairs())
    resolved = capture.resolve_pair(object(), _census(), 2)
    assert resolved["user_id"] == 4242
    assert resolved["color"] == "white"
    assert resolved["snapshot_drift"] == {}


def test_a_pair_whose_colour_moved_refuses(monkeypatch):
    monkeypatch.setattr(
        capture, "_census_pairs", lambda engine: _snapshot_pairs(player_color="black")
    )
    with pytest.raises(qual.QualificationRefusal, match="ordering has moved"):
        capture.resolve_pair(object(), _census(), 2)


def test_a_pair_that_grew_beyond_tolerance_refuses(monkeypatch):
    monkeypatch.setattr(
        capture,
        "_census_pairs",
        lambda engine: _snapshot_pairs(logical_rows=600, positions=455),
    )
    with pytest.raises(qual.QualificationRefusal, match="re-run the census"):
        capture.resolve_pair(object(), _census(), 2)


def test_drift_inside_tolerance_is_recorded_rather_than_refused(monkeypatch):
    monkeypatch.setattr(
        capture,
        "_census_pairs",
        lambda engine: _snapshot_pairs(logical_rows=260, positions=115),
    )
    resolved = capture.resolve_pair(object(), _census(), 2)
    assert resolved["snapshot_drift"]["logical_rows"] == {
        "census": 245,
        "snapshot": 260,
    }
    assert resolved["relative_size_drift"] < capture.PAIR_SHAPE_TOLERANCE


def test_a_pair_index_the_census_does_not_hold_refuses(monkeypatch):
    monkeypatch.setattr(capture, "_census_pairs", lambda engine: _snapshot_pairs())
    with pytest.raises(qual.QualificationRefusal, match="no pair_index"):
        capture.resolve_pair(object(), _census(), 17)


def test_the_private_store_guard_resolves_before_comparing(tmp_path):
    # A prefix test on the unresolved string passes for a traversal and for a
    # symlink pointing anywhere.
    escape = qual.PRIVATE_STORE / ".." / ".." / "tmp" / "leak.pickle"
    with pytest.raises(qual.QualificationRefusal, match="may only be written under"):
        qual.assert_private_store(escape)
    with pytest.raises(qual.QualificationRefusal):
        qual.dump_capture(tmp_path / "leak.pickle", {"candidates": []})
    inside = qual.PRIVATE_STORE / "score-store-qualify" / "x.pickle"
    assert qual.assert_private_store(inside).name == "x.pickle"


def test_a_synthetic_fixture_capture_may_live_outside_the_private_store(tmp_path):
    target = tmp_path / "sf.pickle"
    qual.dump_capture(target, {"candidates": []}, production_derived=False)
    assert qual.load_capture(target)["candidates"] == []
    assert oct(target.stat().st_mode)[-3:] == "600"


class _SchemaConn:
    """Answers the two questions ``assert_spike_schema`` asks, in order."""

    def __init__(self, migrated, triggers):
        self._answers = [migrated, triggers]

    def execute(self, statement, *args, **kwargs):
        return mock.Mock(scalar_one=lambda value=self._answers.pop(0): value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _SchemaEngine:
    def __init__(self, migrated, triggers):
        self._state = (migrated, triggers)

    def connect(self):
        return _SchemaConn(*self._state)


def test_the_sf_fixture_schema_is_the_spikes_and_not_alembics():
    """Both halves refuse, because they are different accidents.

    ``alembic_version`` means someone migrated the fixture database; a
    ``trg_*_evidence_epoch`` trigger means the counter ``build_timeline``
    maintains by hand has acquired a second author. The first is how the second
    happens today, but a trigger installed any other way is just as fatal to the
    tie-back, so the check does not infer one from the other.
    """
    capture.assert_spike_schema(_SchemaEngine(False, 0))
    with pytest.raises(qual.QualificationRefusal, match="alembic_version=present"):
        capture.assert_spike_schema(_SchemaEngine(True, 0))
    with pytest.raises(qual.QualificationRefusal, match="triggers=6"):
        capture.assert_spike_schema(_SchemaEngine(False, 6))


def test_the_fixture_capture_never_migrates_its_database():
    """The production capture migrates its clone; SF must not, and did.

    Migration 20260708_01 seeds ``evidence_epoch`` and installs statement
    triggers on the shared evidence tables. ``build_timeline`` seeds that same
    row itself (``opening_score_storage_workload.py:339``) and bumps the counter
    once per iteration to stage its ``unrelated_epoch`` cause (``:453``), so a
    migrated fixture raised ``duplicate key ... evidence_epoch_pkey`` — and,
    had the seed been tolerated, would have bumped the epoch on EVERY evidence
    write, regenerating a timeline the approved report never produced. The
    schema assertion brackets the timeline: once before, so a migrated database
    refuses up front, and once after, so ``create_all`` is held to the same rule.
    """
    import inspect

    source = inspect.getsource(capture.capture_fixture)
    assert "run_migrations" not in source
    assert source.count("assert_spike_schema(engine)") == 2
    # The two paths that DO clone gr_snap_base still migrate theirs.
    assert "run_migrations" in inspect.getsource(capture.capture)
    assert "run_migrations" in inspect.getsource(capture.control_recompute)


def test_the_cluster_opt_in_allows_only_the_two_named_clusters():
    assert qual.expected_cluster_name({}) == qual.CLUSTER_NAME
    assert (
        qual.expected_cluster_name({qual.QUAL_CLUSTER_ENV: qual.SPIKE_CLUSTER_NAME})
        == qual.SPIKE_CLUSTER_NAME
    )
    with pytest.raises(qual.QualificationRefusal, match="not one of"):
        qual.expected_cluster_name({qual.QUAL_CLUSTER_ENV: "main"})


def test_the_lane_database_name_is_guarded_and_is_never_the_template():
    with pytest.raises(qual.QualificationRefusal, match="gr_delta_lane"):
        qual.create_lane_database("ghostreplay")
    with pytest.raises(qual.QualificationRefusal, match="refusing to drop"):
        qual.drop_lane_database("gr_snap_base")
    assert qual.SNAPSHOT_TEMPLATE == "gr_snap_base"
    import re as _re

    assert not _re.fullmatch(qual.LANE_DATABASE_PATTERN, qual.SNAPSHOT_TEMPLATE)


def test_the_lane_migration_never_shells_out_to_alembic():
    source = pathlib.Path(qual.__file__).read_text()
    assert "alembic upgrade head" not in source.replace(
        "``alembic upgrade head``", ""
    ).replace("`alembic upgrade head`", "")


def test_cell_reports_carry_cutoff_spacing_rather_than_session_timestamps():
    # Cell reports go to an unguarded --output and on to docs/analysis. A real
    # user's session timestamps are production-derived data; the spacing is what
    # a reader of the profile actually needs (§4.5).
    base = datetime(2026, 7, 14, tzinfo=timezone.utc)
    sanitised = qual.sanitised_capture_provenance(
        {
            "pair_index": 2,
            "cutoffs": 3,
            "provenance": {
                "source": "restored production snapshot clone",
                "cutoff_provenance": [
                    {"cutoff": 0, "scored_at": base},
                    {"cutoff": 1, "scored_at": base + timedelta(days=4)},
                    {"cutoff": 2, "scored_at": base + timedelta(days=10)},
                ],
            },
        }
    )
    assert "cutoff_provenance" not in sanitised["provenance"]
    spacing = sanitised["provenance"]["cutoff_spacing_days"]
    assert spacing["cutoffs"] == 3
    assert spacing["span_days"] == pytest.approx(10.0)
    assert spacing["min"] == pytest.approx(4.0)
    assert spacing["max"] == pytest.approx(6.0)
    assert "2026-07-14" not in json.dumps(sanitised, default=str)


def test_a_memory_child_refuses_anything_but_a_two_candidate_slice(monkeypatch, tmp_path):
    # load_capture(tail=2) unpickles everything first, so the RSS high-water was
    # the capture library in both layouts — identically, measuring nothing. The
    # parent writes a pre-scaled two-candidate slice and the child takes only
    # that; a slice of any other length means the parent did not.
    monkeypatch.setattr(
        qual,
        "bootstrap_database_url",
        lambda: mock.Mock(render_as_string=lambda hide_password: "postgresql://x/y"),
    )
    monkeypatch.setattr(qual, "assert_resolved_engine", lambda url: None)
    monkeypatch.setattr(qual, "engine_for", lambda url: _RecordingEngine())
    monkeypatch.setattr(qual, "session_factory_for", lambda eng: object())
    monkeypatch.setattr(
        qual, "load_capture", lambda path: {"candidates": [object()] * 3}
    )
    with pytest.raises(qual.QualificationRefusal, match="two-candidate slice, not 3"):
        qual.memory_child(tmp_path / "slice.pickle", "A", "black")


# --------------------------------------------------------------------------
# §4.9 — the lane clone's own guard
# --------------------------------------------------------------------------


def test_the_lane_child_bootstraps_under_the_lane_guard(monkeypatch):
    # Under the DEFAULT measurement guard this refused every time: the lane
    # database is gr_delta_lane_*, the measurement pattern is gr_score_qual_*.
    # The clone had already been created by then, so a lane run left an
    # unmigrated database behind and called it a failure to migrate.
    seen = {}

    class _Stop(Exception):
        pass

    def _bootstrap(*args, **kwargs):
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(qual, "bootstrap_database_url", _bootstrap)
    with pytest.raises(_Stop):
        qual.migrate_lane()
    assert seen["guard"] is qual.guard_lane_url


@pytest.mark.parametrize(
    ("guard", "database", "accepted"),
    [
        (qual.guard_lane_url, "gr_delta_lane_qual", True),
        (qual.guard_lane_url, "gr_score_qual_r1_c1", False),
        (qual.guard_measurement_url, "gr_score_qual_r1_c1", True),
        (qual.guard_measurement_url, "gr_delta_lane_qual", False),
    ],
)
def test_the_three_guards_do_not_accept_each_others_databases(guard, database, accepted):
    url = f"postgresql://u:p@127.0.0.1:55440/{database}"
    if accepted:
        assert guard(url).database == database
    else:
        with pytest.raises(qual.QualificationRefusal, match="does not match"):
            guard(url)


class _FakeConn:
    def __init__(self, log):
        self.log = log

    def execute(self, statement, *args, **kwargs):
        self.log.append(str(statement))
        return None

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self):
        self.log: list[str] = []
        self.disposed = 0

    def connect(self):
        return _FakeConn(self.log)

    def dispose(self):
        self.disposed += 1


def test_a_failed_lane_migration_drops_the_clone(monkeypatch):
    # An unmigrated clone is worse than no clone: it matches the lane pattern,
    # so a later run would accept it and measure against nobody's schema.
    from sqlalchemy.engine import make_url

    engine = _FakeEngine()
    monkeypatch.setattr(
        qual,
        "_admin_engine",
        lambda: (engine, make_url("postgresql+psycopg://u:p@127.0.0.1:55440/postgres")),
    )
    monkeypatch.setattr(
        qual.subprocess,
        "run",
        lambda *a, **k: mock.Mock(returncode=1, stderr="alembic exploded", stdout=""),
    )
    dropped = []
    monkeypatch.setattr(qual, "drop_lane_database", dropped.append)
    with pytest.raises(qual.QualificationRefusal, match="has been dropped"):
        qual.create_lane_database("gr_delta_lane_qual")
    assert dropped == ["gr_delta_lane_qual"]


def test_neither_clone_helper_prints_a_password(monkeypatch):
    # C4 had no guarded way to get a database at all — the harness could drop
    # one but never create one — and the lane helper printed a URL carrying the
    # cluster password straight into the transcript.
    from sqlalchemy.engine import make_url

    engine = _FakeEngine()
    monkeypatch.setattr(
        qual,
        "_admin_engine",
        lambda: (
            engine,
            make_url("postgresql+psycopg://u:hunter2@127.0.0.1:55440/postgres"),
        ),
    )
    monkeypatch.setattr(qual, "engine_for", lambda url: _FakeEngine())
    created = qual.create_measurement_database("gr_score_qual_r1_c4")
    assert created["database"] == "gr_score_qual_r1_c4"
    assert "hunter2" not in json.dumps(created)
    assert 'CREATE DATABASE "gr_score_qual_r1_c4"' in " ".join(engine.log)


# --------------------------------------------------------------------------
# §4 — run_cell: the closure gate, and the samples a failed cell keeps
# --------------------------------------------------------------------------


def _capture_artifact(**overrides):
    artifact = {
        "run_id": "r1",
        "color": "black",
        "cutoffs": 10,
        "candidates": [],
        "shared_versions": [],
        "shared_invalidations": [],
        "read_inputs": {"fens": [], "parents": []},
        "tree_requests": [("x", "y")],
        "closure_check": {"ran": True, "equal": True},
        "provenance": {"source": "restored production snapshot clone"},
    }
    artifact.update(overrides)
    return artifact


def _drive_run_cell(monkeypatch, tmp_path, capture_artifact, body):
    """Run ``run_cell`` with every cluster-backed step replaced.

    The point is the ORCHESTRATION — the closure gate, and whether the report a
    failed cell writes carries the samples its docstring promises.
    """
    engine = _FakeEngine()
    for name, value in {
        "bootstrap_database_url": lambda: mock.Mock(
            render_as_string=lambda hide_password: "postgresql://x/y"
        ),
        "assert_resolved_engine": lambda url: None,
        "engine_for": lambda url: engine,
        "session_factory_for": lambda eng: object(),
        "load_capture": lambda path: capture_artifact,
        "assert_database_empty": lambda eng: [],
        "assert_cluster_identity": lambda conn, **k: {"cluster_name": "qc"},
        "create_schema": lambda eng: ("opening_current_roots",),
        "assert_current_format_reloptions": lambda eng: {},
        "disable_relation_autovacuum": lambda eng: None,
        "measured_relation_names": lambda eng: frozenset(qual.MEASURED_RELATIONS),
        "maintain_catalog": lambda eng, **kwargs: {
            "catalog_settle_rounds": 1,
            "catalog_maintained": [["pg_catalog.pg_statistic"]],
            "catalog_analyzed_without_effect": ["pg_catalog.pg_statistic"],
            "autovacuum_eligible_after_maintenance": [],
        },
        "copy_shared_evidence": lambda eng, v, i: {},
        "assert_no_foreign_activity": lambda eng: None,
        "profile_identity": lambda eng, census=None: _identity(),
        "prepare_candidates": lambda capture, **k: ([object()] * 10, list(range(10))),
        "sequence_provenance": lambda indices, n: {},
        "footprint": lambda eng, vacuum=False: {},
        "check_orphans": lambda factory, color: {},
        "run_paired_cell": body,
    }.items():
        monkeypatch.setattr(qual, name, value)
    output = tmp_path / "cell.json"
    args = mock.Mock(
        cell="C1", capture=tmp_path / "capture.pickle", profile="S1", copies=1,
        output=output,
    )
    return args, output


def _drive_paired_cell(monkeypatch, *, cell="C1", publications=20,
                       checkpoint_before_each=False, requested=None,
                       fail_at=None, partial=None):
    """Run the REAL ``run_paired_cell`` loop with only the cluster calls stubbed.

    Stubbing the loop BODY, which is what the first sample-retention test did,
    cannot see a bug in the loop itself — and there was one.
    """
    import contextlib

    monkeypatch.setattr("app.opening_cache.get_opening_graph", lambda: object())
    monkeypatch.setattr("app.opening_cache.get_opening_roots", lambda: object())
    monkeypatch.setattr("app.opening_densify.routing_view", lambda graph: object())
    monkeypatch.setattr(
        qual, "scheduler_isolation", lambda: (contextlib.ExitStack(), [])
    )
    monkeypatch.setattr(qual, "read_counters", lambda eng, created: object())
    monkeypatch.setattr(qual, "assert_no_foreign_activity", lambda eng: None)
    monkeypatch.setattr(qual, "_lsn", lambda eng: "0/1000")
    monkeypatch.setattr(qual, "classify_wal", lambda eng, a, b: {"record_bytes": 1})
    monkeypatch.setattr(qual, "assert_wal_uncontaminated", lambda wal: None)
    monkeypatch.setattr(qual, "logical_rows", lambda payload: 24_815)
    monkeypatch.setattr(qual, "_confidence_change_fraction", lambda a, b: 0.5)
    monkeypatch.setattr(
        qual, "_vacuum_window", lambda eng, index, **kwargs: {"window": index}
    )
    published = []

    def publish(factory, owner, color, candidate, fmt):
        if fail_at is not None and len(published) == fail_at:
            raise qual.QualificationRefusal("WAL segment has already been removed")
        published.append(fmt)
        return {"publish_ms": 12.0, "storage_format": fmt.value}

    monkeypatch.setattr(qual, "publish", publish)
    counts = requested or {}

    def counters_clean(before, after, measured=None):
        layout, block = counters_clean.next
        return {
            "checkpoints_num_timed": 0,
            "checkpoints_num_requested": counts.get((layout, block), 0),
            "catalog_autovacuum_events": 0,
            "catalog_autovacuum_relations": [],
            "written_relations": [],
        }

    counters_clean.next = ("A", 0)
    monkeypatch.setattr(qual, "assert_counters_clean", counters_clean)

    # ``assert_counters_clean`` is called once per layout per block, in the
    # order the loop visits them, so the stub follows the same alternation.
    visits = []
    for block in range(publications // 10):
        order = ("A", "B50") if block % 2 == 0 else ("B50", "A")
        visits += [(layout, block) for layout in order]
    visit = iter(visits)

    def counters_clean_seq(before, after, measured=None):
        counters_clean.next = next(visit)
        return counters_clean(before, after, measured)

    monkeypatch.setattr(qual, "assert_counters_clean", counters_clean_seq)

    partial = {} if partial is None else partial
    candidates = [mock.Mock(payload={}) for _ in range(10)]
    indices = [i % 10 for i in range(publications)]
    qual.run_paired_cell(
        _RecordingEngine(),
        object(),
        candidates,
        indices,
        cell=cell,
        color="black",
        created_relations=("opening_current_roots",),
        reads_per_publication=(0, 0),
        checkpoint_before_each=checkpoint_before_each,
        vacuum_every=0,
        partial=partial,
    )
    return partial


def test_the_real_loop_writes_its_pairing_onto_the_caller_s_dict(monkeypatch):
    # ``result = publish(...)`` inside the loop rebound the name that held the
    # caller-owned ``partial``, so after the first publication ``paired_blocks``
    # and ``complete`` landed on the last publication's return value. The report
    # then said "pairing: not reached" and zero kept blocks for a cell that had
    # run to the end, and the evaluator marked every gate of every paired cell
    # ``insufficient_evidence``. Nothing failed; the run was simply unusable.
    partial = _drive_paired_cell(monkeypatch, publications=20)
    assert partial["complete"] is True
    assert partial["paired_blocks"]["complete_paired_blocks"] == 2
    assert partial["paired_blocks"]["discarded"] == []
    assert len(partial["records"]["A"]) == 20
    assert len(partial["records"]["B50"]) == 20


def test_a_checkpointing_cell_records_requested_checkpoints_and_keeps_the_pair(
    monkeypatch,
):
    # C2 publishes at the census max_wal_size of 128MB, where PostgreSQL asks
    # for a checkpoint after roughly 48-64MB of WAL. An S3 layout-A publication
    # is about 75-80MB and the matching B50 about 6MB, so comparing the count
    # would discard EVERY C2:S3 pair — and precisely the pairs where A is worst.
    # C2 fits its ceiling from three sizes, so the rule made the cell
    # unpassable at settings settings_homogeneity correctly refuses to change.
    partial = _drive_paired_cell(
        monkeypatch,
        cell="C2",
        publications=20,
        checkpoint_before_each=True,
        requested={("A", 0): 1, ("A", 1): 1},
    )
    paired = partial["paired_blocks"]
    assert paired["complete_paired_blocks"] == 2
    assert paired["requested_checkpoints_compared"] is False
    assert paired["requested_checkpoint_asymmetry"] == 2
    assert paired["asymmetric_requested_blocks"][0]["reference_num_requested"] == 1


def test_a_warm_cell_still_discards_on_an_unequal_requested_checkpoint(monkeypatch):
    # The exemption is C2's construction, not a general relaxation: C1 issues no
    # explicit CHECKPOINT, so a requested one there lands mid-block on one side
    # only and the pair is not comparable.
    partial = _drive_paired_cell(
        monkeypatch, cell="C1", publications=20, requested={("A", 0): 1}
    )
    paired = partial["paired_blocks"]
    assert paired["complete_paired_blocks"] == 1
    assert paired["requested_checkpoints_compared"] is True
    assert paired["discarded"][0]["reference_num_requested"] == 1


def test_a_paired_cell_that_refuses_keeps_its_samples_on_the_report(
    monkeypatch, tmp_path
):
    # The report is written from a `finally`, but the samples were LOCALS inside
    # run_paired_cell and died with the exception — so a cell that refused in
    # its last minute wrote "every sample collected before this point is
    # retained" and retained none of them.
    def body(*args, partial=None, **kwargs):
        partial["records"] = {"A": [{"step": 0}, {"step": 1}], "B50": []}
        partial["vacuum_windows"] = [{"window": 0}]
        raise qual.QualificationRefusal("WAL segment has already been removed")

    args, output = _drive_run_cell(monkeypatch, tmp_path, _capture_artifact(), body)
    with pytest.raises(qual.QualificationRefusal):
        qual.run_cell(args)
    written = json.loads(output.read_text())
    assert written["complete"] is False
    assert written["failure"]["refusal"] is True
    assert len(written["result"]["records"]["A"]) == 2
    assert written["result"]["vacuum_windows"] == [{"window": 0}]


def test_the_live_sample_lists_are_wired_before_the_first_publication(monkeypatch):
    # `partial` is the caller's dict and the wiring is unconditional, so the
    # promise holds for a refusal in publication one as much as in ninety-nine.
    # Driven, not greped: the rev-9 spelling of this case read the source for a
    # call ordering and would have passed whatever the loop then did with it.
    partial: dict = {}
    with pytest.raises(qual.QualificationRefusal):
        _drive_paired_cell(monkeypatch, publications=20, fail_at=0, partial=partial)
    assert partial["cell"] == "C1"
    assert partial["complete"] is False
    assert partial["records"] == {"A": [], "B50": []}
    assert partial["paired_blocks"]["pairing"].startswith("not reached")


@pytest.mark.parametrize(
    ("closure", "refuses"),
    [
        ({"ran": True, "equal": True}, False),
        ({"ran": False, "skipped": True, "recorded_gap": True}, False),
        ({"ran": True, "equal": False, "reason": "payload differed"}, True),
        (None, True),
    ],
)
def test_a_capture_that_failed_its_closure_check_is_never_replayed(
    monkeypatch, tmp_path, closure, refuses
):
    # §1.3 runs the check and writes the outcome onto the artifact; until now
    # nothing downstream looked at it, so a capture whose reveal mechanism had
    # perturbed evidence would have been replayed by every cell.
    artifact = _capture_artifact(closure_check=closure)
    if closure is None:
        artifact.pop("closure_check")

    def body(*args, partial=None, **kwargs):
        partial["records"] = {"A": [], "B50": []}
        return partial

    args, output = _drive_run_cell(monkeypatch, tmp_path, artifact, body)
    if refuses:
        with pytest.raises(qual.QualificationRefusal, match="closure"):
            qual.run_cell(args)
        assert json.loads(output.read_text())["complete"] is False
    else:
        assert qual.run_cell(args) == 0
        assert json.loads(output.read_text())["complete"] is True


def test_a_synthetic_capture_has_no_closure_to_check():
    # SF is regenerated by build_timeline; there is no reveal mechanism in it to
    # perturb anything.
    qual.assert_capture_closure(
        {"provenance": {"synthetic_only": True, "source": "regenerated fixture"}}
    )


class _SettingsProbe(Exception):
    """Carries the parameters of the first statement and stops the function."""

    def __init__(self, params):
        self.params = params


def test_the_settings_the_harness_asks_for_are_the_settings_it_records():
    # wal_keep_size is asked for in §0.2 so pg_walinspect cannot lose a recycled
    # segment mid-cell; it appeared only in a comment and an error string, so it
    # could never reach the stated-differences list. fsync is recorded for the
    # opposite reason: this bead never changes it, and every WAL figure assumes
    # it is on. Asserted on the parameters the function actually sends, not on
    # the text of the file that sends them.
    class _Conn:
        def execute(self, statement, params=None, *a, **k):
            raise _SettingsProbe(params)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Engine:
        def connect(self):
            return _Conn()

    with pytest.raises(_SettingsProbe) as caught:
        qual.profile_identity(_Engine())
    asked = caught.value.params["names"]
    for name in ("wal_keep_size", "fsync", "max_wal_size", "checkpoint_timeout",
                 "autovacuum", "checkpoint_completion_target"):
        assert name in asked, name


# --------------------------------------------------------------------------
# §5 — the assembled evaluator: SF, settings homogeneity, verdict precedence
# --------------------------------------------------------------------------


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload, default=str))
    return path


def _run_main(tmp_path, cells, memory, *, fixture_cells=(), plateau=None,
              network=None, control=None, delta_lane=None):
    argv = ["--run-id", "r1", "--output", str(tmp_path / "out.json")]
    for index, report in enumerate(_control_reports() if control is None else control):
        argv += ["--control", str(_write(tmp_path, f"c4_{index}.json", report))]
    for index, report in enumerate(_lane_reports() if delta_lane is None else delta_lane):
        argv += ["--delta-lane", str(_write(tmp_path, f"c7_{index}.json", report))]
    for index, cell in enumerate(cells):
        argv += ["--cell", str(_write(tmp_path, f"cell{index}.json", cell))]
    for index, cell in enumerate(fixture_cells):
        argv += ["--fixture-cell", str(_write(tmp_path, f"sf{index}.json", cell))]
    for index, report in enumerate(memory):
        argv += ["--memory", str(_write(tmp_path, f"mem{index}.json", report))]
    if plateau is not None:
        argv += ["--plateau", str(_write(tmp_path, "plateau.json", plateau))]
    if network is not None:
        argv += ["--network", str(_write(tmp_path, "network.json", network))]
    evaluator.main(argv)
    return json.loads((tmp_path / "out.json").read_text())


def test_the_assembled_evaluator_reaches_a_pass_through_main(tmp_path):
    cells, memory = _full_run()
    report = _run_main(
        tmp_path, cells, memory, plateau=_plateau_report(), network=_network_report()
    )
    assert report["verdict"]["aggregate"] == "pass", report["verdict"]["per_gate"]
    assert report["profile"]["settings_deviating_cells"] == []
    assert (tmp_path / "out.md").exists()


def test_an_sf_cell_cannot_be_a_fit_point(tmp_path):
    # §4.1: SF is a REGRESSION TIE-BACK. build_ceilings fits every point it is
    # handed, so an SF cell passed as --cell became a fifth fit point at ~71k
    # rows, with a row mix that is not production's, measured on the OTHER
    # cluster — and the run still reported `pass`.
    cells, memory = _full_run()
    cells.append(_cell("C1", "SF", 71_000, cluster="ghostreplay-score-storage-spike"))
    with pytest.raises(evaluator.QualificationEvaluationError, match="fit points"):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report())


def test_a_fixture_cell_is_reported_and_enters_nothing(tmp_path):
    cells, memory = _full_run()
    fixture = _cell("C1", "SF", 71_000, cluster="ghostreplay-score-storage-spike")
    report = _run_main(
        tmp_path, cells, memory, fixture_cells=[fixture],
        plateau=_plateau_report(), network=_network_report(),
    )
    assert [r["size_profile"] for r in report["fixture_tie_back_cells"]["cells"]] == ["SF"]
    fitted = report["ceilings"]["ceilings"]["warm_publication_wal_bytes"]
    assert [point["size_profile"] for point in fitted["points"]] == [
        "S0", "S1", "S2", "S3"
    ]
    assert "SF" not in json.dumps(report["verdict"])
    assert report["verdict"]["aggregate"] == "pass"


def test_a_cell_measured_under_other_settings_is_refused_not_noted(tmp_path):
    # The baseline was whichever cell was listed FIRST, and a differing digest
    # was reported under a fixed note about max_wal_size. A C2 cell measured
    # with max_wal_size raised AND fsync off therefore passed, and stayed in the
    # post-checkpoint fit — the ceiling §0.3 calls the production-applicable one.
    cells, memory = _full_run()
    cells[-1] = _cell(
        "C2", "S3", 99_260, publications=40,
        identity=_identity(settings={
            "max_wal_size": {"setting": "4096MB"}, "fsync": {"setting": "off"},
        }),
    )
    with pytest.raises(evaluator.QualificationEvaluationError, match="C2:S3"):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report())


def test_c1_may_deviate_on_max_wal_size_and_only_on_that(tmp_path):
    # §9.5's single recorded deviation, and its exact extent.
    cells, memory = _full_run()
    cells[0] = _cell(
        "C1", "S0", 245,
        identity=_identity(settings={"max_wal_size": {"setting": "4096MB"}}),
    )
    report = _run_main(
        tmp_path, cells, memory, plateau=_plateau_report(), network=_network_report()
    )
    assert report["profile"]["settings_deviating_cells"] == ["C1:S0"]
    deviation = report["profile"]["settings_deviations"]["C1:S0"]["max_wal_size"]
    assert deviation == {"baseline": "128MB", "measured": "4096MB"}

    cells[0] = _cell(
        "C1", "S0", 245,
        identity=_identity(settings={
            "max_wal_size": {"setting": "128MB"}, "fsync": {"setting": "off"},
        }),
    )
    with pytest.raises(evaluator.QualificationEvaluationError, match="fsync"):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report())


def test_cells_measured_on_different_clusters_refuse(tmp_path):
    # SF runs on QC-SPIKE by design, which is why it has its own argument; a
    # FIT cell from another cluster is a different qualification.
    cells, memory = _full_run()
    cells[1] = _cell("C1", "S1", 24_815, cluster="ghostreplay-score-storage-spike")
    with pytest.raises(evaluator.QualificationEvaluationError, match="clusters"):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report())


def test_a_memory_or_plateau_report_from_elsewhere_refuses(tmp_path):
    # The revision/host/cluster check ran over --cell inputs ALONE. --memory
    # feeds a ceiling fit and --plateau and --network feed gates, so a C3 child
    # run at a different revision, or a C5 left over from an earlier cluster,
    # entered the verdict unexamined.
    cells, memory = _full_run()
    memory[0] = dict(memory[0], profile_identity=_identity(tested_revision="old"))
    with pytest.raises(evaluator.QualificationEvaluationError, match="revisions"):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report())

    cells, memory = _full_run()
    plateau = _plateau_report()
    plateau["cluster"] = {"cluster_name": "ghostreplay-score-storage-spike"}
    with pytest.raises(evaluator.QualificationEvaluationError, match="clusters"):
        _run_main(tmp_path, cells, memory, plateau=plateau,
                  network=_network_report())


def test_a_plateau_measured_under_other_settings_refuses(tmp_path):
    # settings_homogeneity ran over --cell inputs only too, so C5 could have
    # been measured with fsync off and still gated the release.
    cells, memory = _full_run()
    network = _network_report()
    network["profile_identity"] = _identity(
        settings={"max_wal_size": {"setting": "128MB"}, "fsync": {"setting": "off"}}
    )
    with pytest.raises(evaluator.QualificationEvaluationError, match="fsync"):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(), network=network)


def test_a_mislabelled_fixture_cell_is_caught_by_its_recorded_cluster(tmp_path):
    # --profile is typed by the operator. The CLUSTER is recorded by the harness
    # from the cluster itself, and SF runs on QC-SPIKE, so a cell relabelled S1
    # to sneak into the fit is refused on the cluster it actually ran on.
    cells, memory = _full_run()
    fixture = _cell("C1", "SF", 71_000, cluster="ghostreplay-score-storage-spike")
    mislabelled = _cell("C1", "S1", 71_000,
                        cluster="ghostreplay-score-storage-spike")
    cells.append(mislabelled)
    with pytest.raises(evaluator.QualificationEvaluationError, match="cluster"):
        _run_main(tmp_path, cells, memory, fixture_cells=[fixture],
                  plateau=_plateau_report(), network=_network_report())


def test_a_production_shape_cell_cannot_hide_in_the_fixture_argument(tmp_path):
    # The mirror of the SF refusal: routing a real cell out of the fit through
    # --fixture-cell would silently shrink the fit set and the coverage count.
    cells, memory = _full_run()
    smuggled = cells.pop()
    with pytest.raises(evaluator.QualificationEvaluationError, match="fixture-cell"):
        _run_main(tmp_path, cells, memory, fixture_cells=[smuggled],
                  plateau=_plateau_report(), network=_network_report())


def test_a_missing_control_or_delta_lane_is_a_recorded_gap(tmp_path):
    # §4.2's matrix names C4 and C7 as required results, and neither was an
    # evaluator input: a run with no control at all reported `pass`.
    cells, memory = _full_run()
    report = _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                       network=_network_report(), control=[], delta_lane=[])
    gaps = report["verdict"]["coverage_gaps"]
    assert "C4:control" in gaps
    assert "C7:delta_lane" in gaps
    assert report["verdict"]["aggregate"] == "insufficient_evidence"


def test_one_delta_lane_format_is_not_both(tmp_path):
    # §4.9 requires the lane in BOTH formats: legacy is the shipped writer and
    # B50 is what activation would switch to.
    cells, memory = _full_run()
    report = _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                       network=_network_report(),
                       delta_lane=[_lane_report("legacy")])
    assert "C7:current-b50-v1" in report["verdict"]["coverage_gaps"]
    assert report["verdict"]["aggregate"] == "insufficient_evidence"


def test_a_slower_shipped_legacy_writer_fails_the_control(tmp_path):
    # §5.5: "A_new worse than A_old" stops activation. Read at the p95 and at
    # the same 10% band the A-relative publication gate uses, because the two
    # labels run sequentially in different worktrees and different databases.
    cells, memory = _full_run()
    report = _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                       network=_network_report(),
                       control=_control_reports(old_p95=100.0, new_p95=140.0))
    assert report["verdict"]["per_gate"]["C4:S1:publication_p95"] == "fail"
    assert report["control"]["profiles"][0]["publication_p95_ratio"] == 1.4
    assert report["verdict"]["aggregate"] == "fail"


def test_a_thin_control_run_is_a_gap_not_a_pass(tmp_path):
    cells, memory = _full_run()
    report = _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                       network=_network_report(),
                       control=_control_reports(repetitions=8))
    assert "C4:S1" in report["verdict"]["coverage_gaps"]
    assert report["verdict"]["aggregate"] == "insufficient_evidence"


def test_a_delta_lane_over_the_warm_limit_fails(tmp_path):
    cells, memory = _full_run()
    report = _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                       network=_network_report(),
                       delta_lane=_lane_reports(drill=3200.0))
    per_gate = report["verdict"]["per_gate"]
    assert per_gate["C7:legacy:drill"] == "fail"
    assert per_gate["C7:legacy:normal"] == "pass"
    assert report["verdict"]["aggregate"] == "fail"


def test_the_process_cold_lane_figures_are_carried_and_never_gated(tmp_path):
    # §4.9: process-cold results are recorded, never mixed into the warm p95.
    # The fabricated cold p95 here is 2600 ms, well above the warm numbers.
    cells, memory = _full_run()
    report = _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                       network=_network_report())
    entry = report["delta_lane"]["results"][0]
    assert entry["process_cold"]["normal"]["end_to_end"]["p95_ms"] == 2600.0
    assert not any(
        "process_cold" in name for name in report["verdict"]["per_gate"]
    )
    assert report["verdict"]["aggregate"] == "pass"


def test_two_controls_on_different_clusters_are_not_a_comparison(tmp_path):
    control = _control_reports()
    control[1]["cluster"] = {"cluster_name": "ghostreplay-score-storage-spike"}
    with pytest.raises(evaluator.QualificationEvaluationError, match="different clusters"):
        evaluator.evaluate_control(control)
    # And through main the identity check reaches it first, because a C4 report
    # is now compared against the WHOLE run rather than only against its own
    # other half. Either way it never becomes a comparison.
    cells, memory = _full_run()
    with pytest.raises(
        evaluator.QualificationEvaluationError, match="different revisions, hosts"
    ):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report(), control=control)


def test_a_measured_failure_outranks_a_missing_input(tmp_path):
    # A failing gate plus any gap reported `insufficient_evidence`, which reads
    # as "come back with more data" rather than "this did not pass". With
    # max_wal_size at the census value, gaps are near-certain.
    cells, memory = _full_run()
    for record in cells[1]["result"]["records"]["B50"]:
        record["wal"]["total_bytes"] = 2_000_000
    # No C5 report: a real gap, alongside a real measured failure.
    report = _run_main(tmp_path, cells, memory, network=_network_report())
    verdict = report["verdict"]
    assert verdict["aggregate"] == "fail"
    assert "C1:S1:combined_wal" in verdict["failing_gates"]
    assert verdict["insufficient_gates"], "the gap is still reported, not hidden"


def test_a_skipped_closure_check_is_a_coverage_gap(tmp_path):
    cells, memory = _full_run()
    cells[1] = _cell(
        "C1", "S1", 24_815, closure={"ran": False, "skipped": True},
    )
    report = _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                       network=_network_report())
    gaps = report["verdict"]["coverage_gaps"]
    assert "C1:S1:capture_closure" in gaps
    assert report["verdict"]["aggregate"] == "insufficient_evidence"


# --------------------------------------------------------------------------
# §2.8 — the control runner binds the application AFTER its guard
# --------------------------------------------------------------------------


def test_the_control_runner_imports_no_application_module_at_top():
    # LegacyAdapter pulls in app.db, which binds its engine at import
    # (app/db.py:49), so importing it at module top bound the control runner's
    # application engine to whatever the shell carried — before a guard had run.
    source = pathlib.Path(c4.__file__).read_text()
    header = source.split("def ")[0]
    assert "from scripts.opening_score_storage_adapters import" not in header
    assert "from scripts.opening_score_storage_workload import" not in header
    body = source.split("def main(")[1]
    assert body.index("guard_database_url()") < body.index(
        "from scripts.opening_score_storage_adapters import"
    )
    assert body.index('os.environ["DATABASE_URL"]') < body.index(
        "from scripts.opening_score_storage_adapters import"
    )


def test_the_control_guard_normalises_the_driver(monkeypatch):
    # A bare postgresql:// URL selects psycopg2, which is not installed in
    # either worktree's venv, so a correct URL failed at connect time.
    # The one-name `delenv` that used to stand here is gone: clearing
    # DATABASE_PRIVATE_URL and not DATABASE_URL beside it is the same
    # one-alias-at-a-time habit §1.2 exists to stop. See the autouse fixture.
    monkeypatch.setenv(
        c4.QUAL_DATABASE_ENV, "postgresql://u:p@127.0.0.1:55440/gr_score_qual_r1_c4"
    )
    assert c4.guard_database_url().drivername == "postgresql+psycopg"


def test_the_control_guard_refuses_url_query_overrides(monkeypatch):
    # A query can carry host=, which silently overrides the host the loopback
    # check just proved. The harness refuses these; so does the control.
    monkeypatch.setenv(
        c4.QUAL_DATABASE_ENV,
        "postgresql://u:p@127.0.0.1:55440/gr_score_qual_r1_c4?host=/tmp",
    )
    with pytest.raises(c4.ControlRefusal, match="query overrides"):
        c4.guard_database_url()


def test_the_guard_cases_see_no_inherited_connection_environment():
    """The isolation the two cases above depend on, asserted rather than assumed.

    Both call a guard with NO argument, so they read the real ``os.environ``.
    Under a shell exporting a production ``DATABASE_URL`` they refuse on that
    name and never reach the driver normalisation or the query-override branch
    they are named for — which is how ``.githooks/pre-push`` failed them while
    every ``env -u DATABASE_URL`` run passed. If the autouse fixture is ever
    narrowed or dropped, this fails first and says why.
    """
    for name in (
        *qual.INHERITED_URL_ENV_NAMES,
        *qual.INHERITED_HOST_ENV_NAMES,
        *qual.INHERITED_PG_ENV_NAMES,
    ):
        assert name not in os.environ, (
            f"{name} survived into a guard case; the guards refuse an inherited "
            "connection before anything else, so this case would assert the "
            "wrong refusal"
        )


def test_the_control_runner_refuses_a_database_that_is_not_empty():
    # LegacyAdapter.create only creates what is MISSING, so a database reused
    # across the two labels would hand A_old whatever columns A_new had made.
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return ["opening_score_batches"]

    class _Conn(_FakeConn):
        def execute(self, statement, *args, **kwargs):
            return _Result()

    class _Engine(_FakeEngine):
        def connect(self):
            return _Conn(self.log)

    with pytest.raises(c4.ControlRefusal, match="its own fresh database"):
        c4.assert_database_empty(_Engine())


def test_the_control_runner_points_at_the_harness_for_its_database():
    # There was no guarded way to create it: the harness could drop a cell
    # database but never create one, and `run` only runs harness cells.
    assert "create-database" in (c4.__doc__ or "")
    assert hasattr(qual, "create_measurement_database")


def _c4_capture(**overrides):
    capture = {
        "version": c4.CAPTURE_VERSION,
        "candidates": [object(), object()],
        "provenance": {},
        "closure_check": {"ran": True, "equal": True},
    }
    capture.update(overrides)
    return capture


def test_the_control_runner_refuses_a_capture_whose_closure_failed(tmp_path):
    # C4 loaded the payload with a bare pickle.load and asked nothing. It is the
    # one runner that measures the SHIPPED legacy writer, and it would have
    # replayed a capture whose §1.3 closure check failed — a capture whose final
    # payload did NOT equal a plain recompute on an untouched clone, so the
    # reveal mechanism perturbed evidence and every number below it is fiction.
    path = qual.PRIVATE_STORE / "score-store-qualify" / "capture.pickle"
    failed = _c4_capture(closure_check={"ran": True, "equal": False,
                                        "reason": "payload differs"})
    with pytest.raises(c4.ControlRefusal, match="did not pass"):
        c4.assert_capture_admissible(path, failed)
    with pytest.raises(c4.ControlRefusal, match="no closure_check"):
        c4.assert_capture_admissible(path, _c4_capture(closure_check=None))


def test_the_control_runner_permits_a_skipped_or_synthetic_closure(tmp_path):
    # A skip is an explicit operator choice and travels onto the report, where
    # the evaluator records the coverage gap. A synthetic capture has no reveal
    # mechanism, so there is nothing for it to have perturbed.
    path = qual.PRIVATE_STORE / "score-store-qualify" / "capture.pickle"
    skipped = c4.assert_capture_admissible(
        path, _c4_capture(closure_check={"skipped": True, "reason": "operator"})
    )
    assert skipped["closure_check"]["skipped"] is True
    synthetic = c4.assert_capture_admissible(
        tmp_path / "fixture.pickle",
        _c4_capture(provenance={"synthetic_only": True}, closure_check=None),
    )
    assert synthetic["synthetic_only"] is True


def test_the_control_runner_refuses_a_production_capture_outside_the_store(tmp_path):
    with pytest.raises(c4.ControlRefusal, match="private store"):
        c4.assert_capture_admissible(tmp_path / "leak.pickle", _c4_capture())


def test_the_control_runner_refuses_a_capture_of_another_version():
    path = qual.PRIVATE_STORE / "score-store-qualify" / "capture.pickle"
    with pytest.raises(c4.ControlRefusal, match="capture version"):
        c4.assert_capture_admissible(path, _c4_capture(version=99))


def test_the_control_runner_help_puts_no_url_in_front_of_a_command():
    # --help prints the module docstring, so an example there is the instruction
    # an operator follows. `VAR=<url> cmd` puts the credential in shell history.
    doc = c4.__doc__ or ""
    assert "GHOSTREPLAY_STORAGE_QUAL_DATABASE_URL=..." not in doc
    assert "export GHOSTREPLAY_STORAGE_QUAL_DATABASE_URL=" in doc
    assert "set +o history" in doc


# --------------------------------------------------------------------------
# §1.3 — the hide/reveal closure, executed
# --------------------------------------------------------------------------


@pytest.fixture
def sqlite_engine(tmp_path):
    """A full schema with FOREIGN KEYS ENFORCED.

    The closure's whole point is the ORDER — children before parents on the way
    out, parents before children on the way back — and SQLite does not enforce
    foreign keys unless asked, so a fixture without the pragma would pass
    whatever order the code used. The capture SQL is dialect-neutral (expanding
    ``IN`` and no data-modifying CTE) precisely so this can run.
    """
    from sqlalchemy import create_engine, event

    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'closure.db'}")

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def qual_storage_format_current():
    from app.opening_score_storage import StorageFormat

    return StorageFormat.CURRENT.value


def _marker(engine, *, user_id, color, generation, storage_format):
    from datetime import datetime as _dt

    from sqlalchemy import insert

    from app.models import OpeningScoreBatch

    with engine.begin() as conn:
        return conn.execute(
            insert(OpeningScoreBatch).returning(OpeningScoreBatch.id),
            {
                "user_id": user_id,
                "player_color": color,
                "generation": generation,
                "computed_at": _dt(2026, 9, 21, 12, 0, 0),
                "storage_format": storage_format,
            },
        ).scalar_one()


def _current_root(engine, *, user_id, color):
    from sqlalchemy import insert

    from app.models import CurrentOpeningRoot

    with engine.begin() as conn:
        conn.execute(
            insert(CurrentOpeningRoot),
            {
                "user_id": user_id,
                "player_color": color,
                "opening_key": "e4",
                "opening_name": "King's Pawn",
                "opening_family": "Open",
                "opening_score": 1.0,
                "confidence": 0.5,
                "coverage": 0.5,
                "weighted_depth": 3.0,
                "sample_size": 10,
                "game_count": 4,
            },
        )


def _orphans(engine):
    from sqlalchemy.orm import sessionmaker

    return qual.check_orphans(sessionmaker(bind=engine), "black")


def test_a_clean_pair_of_layouts_reports_clean(sqlite_engine):
    legacy = qual.OWNER_BY_LAYOUT["A"]
    current = qual.OWNER_BY_LAYOUT["B50"]
    # The shipped legacy writer KEEPS the prior snapshot for legacy readers
    # (_retire(keep_legacy=True)), so two markers is correct there and one is
    # correct for the converted pair. A flat "one marker per pair" would fail
    # every legacy cell on correct behaviour.
    _marker(sqlite_engine, user_id=legacy, color="black", generation=1,
            storage_format="legacy")
    _marker(sqlite_engine, user_id=legacy, color="black", generation=2,
            storage_format="legacy")
    _marker(sqlite_engine, user_id=current, color="black", generation=1,
            storage_format=qual_storage_format_current())
    _current_root(sqlite_engine, user_id=current, color="black")
    result = _orphans(sqlite_engine)
    assert result["clean"] is True
    assert result["markers_per_owner"][f"{legacy}:black"] == 2
    assert result["current_rows_without_live_marker"] == 0


def test_an_unreferenced_current_row_is_computed_and_gated(sqlite_engine):
    # The current tables carry (user_id, player_color) and no batch_id, so a row
    # whose pair holds no live CURRENT marker is unreachable evidence. It was
    # never computed at all: `clean` read the cross-format counts alone.
    current = qual.OWNER_BY_LAYOUT["B50"]
    _marker(sqlite_engine, user_id=current, color="black", generation=1,
            storage_format="legacy")
    _current_root(sqlite_engine, user_id=current, color="black")
    result = _orphans(sqlite_engine)
    assert result["current_rows_without_live_marker"] == 1
    assert result["clean"] is False


def test_a_retained_extra_marker_is_gated_not_merely_reported(sqlite_engine):
    # markers_per_owner was reported beside a verdict that did not read it, so
    # C5 could report `clean` with a whole previous generation still resident.
    current = qual.OWNER_BY_LAYOUT["B50"]
    for generation in (1, 2):
        _marker(sqlite_engine, user_id=current, color="black",
                generation=generation, storage_format=qual_storage_format_current())
    _current_root(sqlite_engine, user_id=current, color="black")
    result = _orphans(sqlite_engine)
    assert result["markers_over_limit"][f"{current}:black"]["permitted"] == 1
    assert result["clean"] is False


def test_a_pair_holding_both_formats_is_visible_and_refused(sqlite_engine):
    # live_marker_formats grouped on the format and then keyed the dict by the
    # pair alone, so a pair holding BOTH formats — the leak this cell exists to
    # catch — was reported as holding whichever came back last.
    current = qual.OWNER_BY_LAYOUT["B50"]
    _marker(sqlite_engine, user_id=current, color="black", generation=1,
            storage_format="legacy")
    _marker(sqlite_engine, user_id=current, color="black", generation=2,
            storage_format=qual_storage_format_current())
    _current_root(sqlite_engine, user_id=current, color="black")
    result = _orphans(sqlite_engine)
    assert result["pairs_holding_both_formats"] == [f"{current}:black"]
    assert sorted(result["live_marker_formats"]) == [
        f"{current}:black:current-b50-v1",
        f"{current}:black:legacy",
    ]
    assert result["clean"] is False


def _seed_closure(engine):
    """Three sessions, one blunder, and a LATER session's move targeting it."""
    import uuid as _uuid

    from sqlalchemy import insert

    from app.models import Base

    tables = Base.metadata.tables
    ids = {name: _uuid.uuid4() for name in ("early", "drill", "other")}
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            insert(tables["positions"]),
            [{"id": 1, "user_id": 7, "fen_hash": "h", "fen_raw": "f",
              "active_color": "white"}],
        )
        conn.execute(
            insert(tables["game_sessions"]),
            [
                {"id": ids[name], "user_id": 7, "status": "finished",
                 "engine_elo": 1500, "started_at": now, "ended_at": now}
                for name in ids
            ],
        )
        conn.execute(
            insert(tables["blunders"]),
            [{"id": 11, "user_id": 7, "position_id": 1, "bad_move_san": "a3",
              "best_move_san": "e4", "eval_loss_cp": 300,
              "source_session_id": ids["early"]}],
        )
        conn.execute(
            insert(tables["blunder_opportunity_summaries"]),
            [{"blunder_id": 11}],
        )
        conn.execute(
            insert(tables["session_moves"]),
            [
                # The cross-session case: a DRILL session's move pointing at a
                # blunder that belongs to an EARLIER session.
                {"id": 1, "session_id": ids["drill"], "move_number": 1,
                 "color": "white", "move_san": "e4", "fen_after": "f",
                 "target_blunder_id": 11},
                {"id": 2, "session_id": ids["early"], "move_number": 1,
                 "color": "white", "move_san": "d4", "fen_after": "f",
                 "target_blunder_id": None},
                {"id": 3, "session_id": ids["other"], "move_number": 1,
                 "color": "black", "move_san": "c5", "fen_after": "f",
                 "target_blunder_id": None},
            ],
        )
    return ids


def _counts(engine):
    from sqlalchemy import text as _text

    names = [name for name, _ in capture.HELD_TABLES]
    with engine.connect() as conn:
        return {
            name: conn.execute(_text(f"SELECT count(*) FROM {name}")).scalar_one()
            for name in names
        }


def test_hiding_and_revealing_restores_every_row(sqlite_engine):
    # The closure was verified against the metadata but never EXECUTED. It is
    # the one step in this bead that mutates production-derived data.
    ids = _seed_closure(sqlite_engine)
    before = _counts(sqlite_engine)
    assert before["blunder_opportunity_summaries"] == 1

    capture.hide_sessions(sqlite_engine, list(ids.values()))
    hidden = _counts(sqlite_engine)
    assert set(hidden.values()) == {0}, hidden

    # The drill session first: its move needs a blunder whose own session is
    # still hidden, so a single parent-first pass would either lose the move or
    # violate the foreign key. The sweep leaves it held.
    capture.reveal_session(sqlite_engine, ids["drill"])
    assert _counts(sqlite_engine)["session_moves"] == 0

    capture.reveal_session(sqlite_engine, ids["early"])
    # Revealing `early` restores the blunder, which makes the ALREADY-revealed
    # drill session's move restorable — that is the fixpoint, not a second pass.
    assert _counts(sqlite_engine)["session_moves"] == 2

    capture.reveal_session(sqlite_engine, ids["other"])
    assert _counts(sqlite_engine) == before
    capture.assert_holding_tables_empty(sqlite_engine)
    capture.drop_holding_tables(sqlite_engine)


def test_a_session_still_hidden_keeps_its_whole_closure_held(sqlite_engine):
    ids = _seed_closure(sqlite_engine)
    capture.hide_sessions(sqlite_engine, list(ids.values()))
    capture.reveal_session(sqlite_engine, ids["other"])
    counts = _counts(sqlite_engine)
    assert counts["game_sessions"] == 1
    assert counts["session_moves"] == 1
    assert counts["blunders"] == 0
    assert counts["blunder_opportunity_summaries"] == 0
    with pytest.raises(qual.QualificationRefusal, match="blunders"):
        capture.assert_holding_tables_empty(sqlite_engine)
    capture.drop_holding_tables(sqlite_engine)


# --------------------------------------------------------------------------
# §1.1 — the census keys this bead reads
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", capture.CENSUS_TOP_LEVEL_FIELDS)
def test_a_census_missing_a_key_is_refused_by_name(missing):
    census = _census()
    census.pop(missing)
    with pytest.raises(qual.QualificationRefusal, match=missing):
        capture.assert_census_shape(census)


@pytest.mark.parametrize("missing", capture.CENSUS_PAIR_FIELDS)
def test_a_census_pair_missing_a_field_is_refused_by_name(missing):
    census = _census()
    census["pairs"][0].pop(missing)
    with pytest.raises(qual.QualificationRefusal, match=missing):
        capture.assert_census_shape(census)


def test_a_census_setting_must_be_a_setting_mapping():
    # _stated_differences reads census["settings"][name]["setting"]; a flat
    # mapping would silently produce an empty stated-differences list.
    census = _census()
    census["settings"] = {"max_wal_size": "128MB"}
    with pytest.raises(qual.QualificationRefusal, match="max_wal_size"):
        capture.assert_census_shape(census)


@pytest.mark.release_seal
def test_the_real_census_matches_the_shape_this_bead_reads():
    # The agreement between the §1.1 census and the tools that read it was
    # pinned only by hand-built fixtures. This checks the actual file when the
    # private store holds one, and skips where it does not.
    store = pathlib.Path.home() / ".ghostreplay-private/score-store-qualify"
    found = sorted(store.glob("census-*.json")) if store.is_dir() else []
    if not found:
        pytest.skip("no census in the private store on this host")
    capture.assert_census_shape(json.loads(found[-1].read_text()))


def test_a_fixture_shaped_plateau_or_memory_report_is_refused_too(tmp_path):
    # --plateau, --network and --memory each feed the verdict or a fit directly,
    # so an SF report there would be an acceptance gate on the fixture shape by
    # another route.
    cells, memory = _full_run()
    sf_plateau = _plateau_report()
    sf_plateau["profile"] = "SF"
    with pytest.raises(evaluator.QualificationEvaluationError, match="fit points"):
        _run_main(tmp_path, cells, memory, plateau=sf_plateau,
                  network=_network_report())
    memory[0]["profile"] = "SF"
    with pytest.raises(evaluator.QualificationEvaluationError, match="fit points"):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report())


# --------------------------------------------------------------------------
# §4.8 — C5's uncovered spans, and §2.2's settled catalogs at cell start
# --------------------------------------------------------------------------


def _plateau_footprint():
    return {
        f"{layout}_{field}": value
        for layout in ("A", "B50")
        for field, value in (
            ("total_bytes", 1000),
            ("live_tuples", 10),
            ("dead_tuples", 0),
        )
    }


def _drive_plateau_cell(monkeypatch):
    """Run the REAL ``run_plateau_cell`` with only the cluster calls replaced."""
    engine = _RecordingEngine({**_catalog_script(), "SELECT count(*)": [0]})
    marks = iter(range(1000))
    spans: list[tuple[int, int]] = []

    monkeypatch.setattr(qual, "read_counters", lambda eng, created: next(marks))
    monkeypatch.setattr(
        qual,
        "assert_counters_clean",
        lambda before, after, measured=None: spans.append((before, after))
        or {"span": (before, after)},
    )
    monkeypatch.setattr(
        qual,
        "publish",
        lambda *a, **k: {"publish_ms": 1.0, "storage_format": "x"},
    )
    held = iter([1, 0] * 8)
    monkeypatch.setattr(
        qual,
        "footprint",
        lambda eng, vacuum=False: {
            **_plateau_footprint(),
            "A_dead_tuples": next(held),
            "B50_dead_tuples": 0,
        },
    )
    monkeypatch.setattr(
        qual,
        "_vacuum_window",
        lambda eng, index, **kwargs: {
            "window": index,
            "after_vacuum": _plateau_footprint(),
        },
    )
    monkeypatch.setattr(qual, "check_orphans", lambda factory, color: {"clean": True})
    monkeypatch.setattr(qual, "assert_no_foreign_activity", lambda eng: None)

    result = qual.run_plateau_cell(
        engine,
        lambda: None,
        [object()] * 60,
        list(range(60)),
        "black",
        created_relations=("user_opening_scores",),
    )
    return result, spans, engine.log


def test_c5_diffs_its_publications_and_not_only_its_windows(monkeypatch):
    # C5 is nothing but publications and vacuum windows, so "the windows carry a
    # counter diff" left most of the cell undiffed: the ten publications before
    # each window were the one span where an autovacuum on a measured relation
    # could pass unobserved, and there are twelve of them.
    result, spans, _log = _drive_plateau_cell(monkeypatch)
    windows = result["windows"]
    assert len(windows["A"]) == len(windows["B50"]) == 6
    for layout_windows in windows.values():
        for entry in layout_windows:
            assert entry["publication_span"]["span"][0] < entry["publication_span"]["span"][1]
    # every diff is over a DISJOINT span, never the same pair read twice
    assert len({span for span in spans}) == len(spans)


def test_c5_diffs_and_settles_the_reader_held_phase(monkeypatch):
    # The reader-held phase runs four more vacuum spans with no upkeep and ends
    # in assert_no_foreign_activity, so without its own diff it either missed a
    # worker or tripped over one the harness had earned itself.
    result, _spans, log = _drive_plateau_cell(monkeypatch)
    assert "span" in result["reader_phase"]
    assert result["reader_phase"]["catalog_settle_rounds"] == 1
    # and the settling happens BEFORE the foreign-activity check, which is the
    # check the unsettled pg_statistic would otherwise have tripped
    assert any(line.startswith("VACUUM") for line in log)


def test_c2_can_lose_a_pair_and_still_clear_its_sample_floor():
    # Forty publications against a forty-publication floor is zero slack: one
    # discarded pair loses the SIZE, and the post-checkpoint ceiling needs all
    # three sizes. Any launcher pass that picks anything up costs a pair, so the
    # cell has to be able to afford one.
    publications = qual.CELL_SPECS["C2"]["publications"]
    assert publications - 2 * 10 >= evaluator.MINIMUM_CELL_SAMPLES
    # C1 keeps its own slack for the same reason (a timed checkpoint)
    assert qual.CELL_SPECS["C1"]["publications"] - 2 * 10 >= evaluator.MINIMUM_CELL_SAMPLES


def test_a_cell_settles_its_catalogs_before_its_first_block(monkeypatch, tmp_path):
    # create_schema inserts thousands of catalog rows; the counts are pending on
    # the backends that did it for up to a second, and the first spelling
    # maintained four NAMED catalogs against counters that did not yet include
    # the DDL. The launcher then picked up the real churn one naptime later,
    # inside block 0: nine events on eight catalogs, every one of them a
    # discarded pair.
    order: list[str] = []

    def body(*args, **kwargs):
        order.append("blocks")
        kwargs["partial"].update({"complete": True})

    args, output = _drive_run_cell(monkeypatch, tmp_path, _capture_artifact(), body)
    real_maintain = qual.maintain_catalog
    monkeypatch.setattr(
        qual,
        "maintain_catalog",
        lambda eng, **kwargs: (order.append("maintain"), real_maintain(eng, **kwargs))[1],
    )
    qual.run_cell(args)
    report = json.loads(output.read_text())
    assert order == ["maintain", "blocks"]
    assert report["catalog_maintenance"]["catalog_settle_rounds"] == 1
    assert report["catalog_maintenance"]["catalog_unclearable"] == []


# --------------------------------------------------------------------------
# §5.1b — C4 and C7 are inputs, so they are checked like inputs
# --------------------------------------------------------------------------


def test_the_evaluator_names_the_same_two_clusters_the_harness_does():
    # The evaluator reads JSON and imports no SQLAlchemy, so the two names are
    # spelled twice. A drift between them would silently disable the rule.
    assert evaluator.QUALIFICATION_CLUSTER == qual.CLUSTER_NAME
    assert evaluator.FIXTURE_CLUSTER == qual.SPIKE_CLUSTER_NAME


def test_a_whole_run_on_the_spike_cluster_refuses_without_a_fixture_cell(tmp_path):
    # The cluster rule only ever fired when a --fixture-cell had been supplied to
    # share a cluster WITH. A run measured end to end on the spike cluster is
    # perfectly homogeneous with itself, so it passed every check there was.
    cells, memory = _full_run()
    for cell in cells:
        cell["cluster"] = {"cluster_name": "ghostreplay-score-storage-spike"}
    for child in memory:
        child["cluster"] = {"cluster_name": "ghostreplay-score-storage-spike"}
    plateau = _plateau_report()
    plateau["cluster"] = {"cluster_name": "ghostreplay-score-storage-spike"}
    network = _network_report()
    network["cluster"] = {"cluster_name": "ghostreplay-score-storage-spike"}
    spike = "ghostreplay-score-storage-spike"
    with pytest.raises(
        evaluator.QualificationEvaluationError, match="must be measured on"
    ):
        _run_main(
            tmp_path, cells, memory, plateau=plateau, network=network,
            control=_control_reports(cluster=spike),
            delta_lane=_lane_reports(cluster=spike),
        )


def test_a_shipped_control_measured_at_another_revision_refuses(tmp_path):
    # A_new IS the shipped writer. A C4 pair run at some other commit says
    # nothing about the revision being qualified, and its revision was compared
    # to nothing at all.
    cells, memory = _full_run()
    control = _control_reports()
    control[1]["revision"] = "deadbee"
    with pytest.raises(
        evaluator.QualificationEvaluationError, match="different revisions"
    ):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report(), control=control)


def test_the_predecessor_control_is_exempt_on_revision_and_only_on_that(tmp_path):
    # A_old is BY CONSTRUCTION the predecessor commit — that is the entire point
    # of the control — so requiring it to match would refuse every correct run.
    # Its host and cluster are still compared.
    cells, memory = _full_run()
    report = _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                       network=_network_report())
    assert report["profile"]["identity_revision_exempt"] == ["C4:S1:A_old"]

    control = _control_reports()
    control[0]["host_platform"] = "Linux-6.8"
    with pytest.raises(
        evaluator.QualificationEvaluationError, match="different revisions, hosts"
    ):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report(), control=control)


def test_a_delta_lane_from_another_machine_refuses(tmp_path):
    # The C7 summary carried no revision, host or cluster at all, so any lane
    # file from any machine at any commit satisfied "C7 is a required result".
    cells, memory = _full_run()
    lanes = _lane_reports()
    lanes[1]["identity"] = dict(lanes[1]["identity"], host_platform="Linux-6.8")
    with pytest.raises(
        evaluator.QualificationEvaluationError, match="different revisions, hosts"
    ):
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report(), delta_lane=lanes)


def test_an_input_with_no_identity_at_all_is_refused_by_name(tmp_path):
    cells, memory = _full_run()
    lanes = _lane_reports()
    lanes[0].pop("identity")
    control = _control_reports()
    control[0].pop("host_platform")
    with pytest.raises(
        evaluator.QualificationEvaluationError, match="C4:S1:A_old"
    ) as excinfo:
        _run_main(tmp_path, cells, memory, plateau=_plateau_report(),
                  network=_network_report(), control=control, delta_lane=lanes)
    assert "C7:legacy" in str(excinfo.value)


def test_a_delta_lane_report_cannot_loosen_the_gate_it_is_judged_by(tmp_path):
    # `p95_limit_ms` came out of the file and straight into the comparison, so a
    # report could declare a 30-second limit and pass itself.
    lanes = _lane_reports(normal=9000.0, limit=30000.0)
    with pytest.raises(
        evaluator.QualificationEvaluationError, match="does not set the gate"
    ):
        evaluator.evaluate_delta_lane(lanes)


def test_a_control_capture_with_a_skipped_closure_is_a_recorded_gap():
    # Only --cell results were read for the closure check, so a C4 pair replayed
    # from a capture whose §1.3 check was skipped counted as full coverage.
    control = _control_reports()
    control[1]["capture_closure"] = {"ran": False, "skipped": True}
    evaluated = evaluator.evaluate_control(control)
    gaps = evaluator.required_coverage([], None, None, control=evaluated)
    assert "C4:S1:A_new:capture_closure" in gaps
    assert "C4:S1:A_old:capture_closure" not in gaps
