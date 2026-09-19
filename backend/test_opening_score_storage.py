"""Storage behavior independent of scoring and reader orchestration."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import CurrentOpeningPosition, OpeningScoreBatch
from app.opening_cache import reserve_opening_score_generation
from app.opening_score_storage import (
    PublicationSuperseded,
    RetiredScoreHandle,
    ScoreHandle,
    ScorePayload,
    StorageFormat,
    _GROUPS,
    convert_pair,
    handle_is_live,
    payload_query,
    publication_lock_key,
    publish_scores,
    read_payload,
)

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def candidate(db, owner=123, color="white", **metadata):
    generation = reserve_opening_score_generation(db, owner, color)
    return OpeningScoreBatch(
        user_id=owner,
        player_color=color,
        generation=generation,
        computed_at=NOW,
        evidence_seq=metadata.pop("evidence_seq", 7),
        cache_epoch=11,
        scoped_shared_digest="scope",
        **metadata,
    )


def payload():
    # Populate every semantic column, including all nullable branch summaries.
    groups = {}
    for name, table, _, _ in _GROUPS:
        row = {}
        for c in table.columns:
            if c.name in {"id", "user_id", "player_color"}:
                continue
            row[c.name] = {str: "key", int: 2, float: 0.25, bool: True, datetime: NOW}[
                c.type.python_type
            ]
        if name == "scope":
            row.update(kind="raw", fen="fen")
        groups[name] = (row,)
    return ScorePayload(**groups)


def publish(db, value=None, *, owner=123, color="white", format=StorageFormat.CURRENT):
    return publish_scores(
        db,
        candidate(db, owner, color),
        value if value is not None else payload(),
        storage_format=format,
    )


def load(db, batch):
    return read_payload(db, ScoreHandle.from_batch(batch))


@pytest.mark.parametrize(
    "user,white",
    [(1, -1780102420), (2147483648, 1219065628), (9223372036854775807, -1086908174)],
)
def test_lock_goldens(user, white):
    assert publication_lock_key(user, "white") == (1196577603, white)
    assert publication_lock_key(user, "black") == (1196577603, white + 1)


@pytest.mark.parametrize("user", [True, 1.0, "1", -(2**63) - 1, 2**63])
def test_lock_rejects_invalid_identity(user):
    with pytest.raises(ValueError):
        publication_lock_key(user, "white")


def test_lock_full_int64_and_color():
    assert publication_lock_key(-(2**63), "white") != publication_lock_key(0, "white")
    with pytest.raises(ValueError):
        publication_lock_key(1, "WHITE")


def test_current_exact_diff_stable_ids_and_atomic_retirement(db_session):
    db = db_session
    first = publish(db)
    old_handle = ScoreHandle.from_batch(first)
    before = db.execute(select(CurrentOpeningPosition)).scalar_one().id
    statements = []

    def record(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", record)
    try:
        second = publish(db)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", record)
    assert not any(
        s.startswith(
            (
                "INSERT INTO opening_current",
                "UPDATE opening_current",
                "DELETE FROM opening_current",
            )
        )
        for s in statements
    )
    assert second.id > old_handle.batch_id
    assert not handle_is_live(db, old_handle)
    with pytest.raises(RetiredScoreHandle):
        read_payload(db, old_handle)
    assert list(db.execute(payload_query(old_handle, "positions"))) == []
    assert db.execute(select(CurrentOpeningPosition)).scalar_one().id == before
    assert load(db, second) == payload()
    value = payload()
    changed = replace(
        value, positions=({**value.positions[0], "confidence": 0.250000000000001},)
    )
    third = publish(db, changed)
    assert load(db, third) == changed
    assert db.execute(select(CurrentOpeningPosition)).scalar_one().id == before


FIELDS = [
    (name, c.name)
    for name, table, _, keys in _GROUPS
    for c in table.columns
    if c.name not in {"id", "user_id", "player_color", *keys}
]


@pytest.mark.parametrize("group,field", FIELDS)
def test_every_semantic_field_is_persisted_exactly(db_session, group, field):
    first = publish(db_session)
    value = load(db_session, first)
    row = dict(getattr(value, group)[0])
    old = row[field]
    row[field] = (
        not old
        if type(old) is bool
        else old + 1
        if type(old) in {int, float}
        else old + timedelta(microseconds=1)
        if isinstance(old, datetime)
        else old + "-changed"
    )
    changed = replace(value, **{group: (row,)})
    second = publish(db_session, changed)
    assert load(db_session, second) == changed


def test_null_empty_quarantine_recovery_and_owner_color_isolation(db_session):
    db = db_session
    one = publish(db)
    other = publish(db, owner=456)
    black = publish(db, color="black")
    value = payload()
    row = dict(
        value.positions[0],
        has_evidence=False,
        opening_score=None,
        confidence=None,
        coverage=None,
        weighted_depth=None,
        sample_size=0,
        game_count=0,
        last_practiced_at=None,
    )
    nulls = replace(value, positions=(row,), roots=(), scope=())
    one = publish(db, nulls)
    assert load(db, one) == nulls
    # All quarantined scores can leave edges and a valid empty scope.
    edges_only = replace(nulls, positions=())
    one = publish(db, edges_only)
    assert load(db, one) == edges_only
    empty = publish(db, ScorePayload())
    assert load(db, empty) == ScorePayload()
    assert load(db, other) == payload()
    assert load(db, black) == payload()
    recovered = publish(db)
    assert load(db, recovered) == payload()


def test_forward_reverse_conversion_preserves_evidence_and_clears_obsolete_rows(
    db_session,
):
    db = db_session
    first = publish(db, format=StorageFormat.LEGACY)
    second = publish(db, format=StorageFormat.LEGACY)
    first_handle = ScoreHandle.from_batch(first)
    second_handle = ScoreHandle.from_batch(second)
    second_evidence, second_time, second_fp = (
        second.evidence_seq,
        second.computed_at,
        second.registry_fingerprint,
    )
    db.rollback()
    converted = convert_pair(db, 123, "white", StorageFormat.CURRENT)
    assert converted.evidence_seq == second_evidence
    assert converted.computed_at == second_time
    assert converted.registry_fingerprint == second_fp
    assert load(db, converted) == payload()
    assert not handle_is_live(db, first_handle)
    assert not handle_is_live(db, second_handle)
    converted_handle = ScoreHandle.from_batch(converted)
    db.rollback()
    reversed_batch = convert_pair(db, 123, "white", StorageFormat.LEGACY)
    assert load(db, reversed_batch) == payload()
    for _, table, legacy, _ in _GROUPS:
        assert list(db.execute(select(table))) == []
        assert len(list(db.execute(select(legacy)))) == 1
    assert not handle_is_live(db, converted_handle)


def test_supersession_is_not_evidence_order(db_session):
    db = db_session
    older_reservation = candidate(db, evidence_seq=999)
    newer_reservation = candidate(db, evidence_seq=1)
    latest = publish_scores(
        db, newer_reservation, payload(), storage_format=StorageFormat.CURRENT
    )
    db.rollback()
    with pytest.raises(PublicationSuperseded) as caught:
        publish_scores(
            db, older_reservation, ScorePayload(), storage_format=StorageFormat.CURRENT
        )
    assert caught.value.batch.id == latest.id
    assert (
        caught.value.batch.evidence_seq == 1
    )  # never rewrite evidence from generation
    assert load(db, latest) == payload()


@pytest.mark.parametrize(
    "stage",
    [
        "opening_current_roots",
        "opening_current_positions",
        "opening_current_edges",
        "opening_current_scope",
        "INSERT INTO opening_score_batches",
        "DELETE FROM opening_score_batches",
    ],
)
def test_atomic_failure_boundaries(db_session, stage):
    db = db_session
    first = publish(db)
    batch = candidate(db)
    value = ScorePayload()

    def fail(conn, cursor, statement, parameters, context, many):
        if stage in statement and statement.startswith(("DELETE", "INSERT", "UPDATE")):
            raise RuntimeError("injected publication failure")

    event.listen(db.get_bind(), "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            publish_scores(db, batch, value, storage_format=StorageFormat.CURRENT)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", fail)
    assert load(db, first) == payload()
    assert len(list(db.scalars(select(OpeningScoreBatch)))) == 1


def test_payload_rejects_missing_duplicate_and_wrong_types():
    value = payload()
    with pytest.raises(ValueError, match="duplicate"):
        replace(value, roots=value.roots * 2)
    row = dict(value.positions[0])
    row.pop("confidence")
    with pytest.raises(ValueError, match="incomplete"):
        replace(value, positions=(row,))
    with pytest.raises(TypeError):
        replace(value, positions=({**value.positions[0], "game_count": True},))
    with pytest.raises(TypeError):
        value.positions[0]["confidence"] = 1.0
    aware = {
        **value.positions[0],
        "last_practiced_at": NOW.astimezone(timezone(timedelta(hours=3))),
    }
    assert replace(value, positions=(aware,)) == value


def test_commit_failure_rolls_back_on_file_database(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'scores.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            first = publish(db)
            handle = ScoreHandle.from_batch(first)
            batch = candidate(db)
            original = Session.commit

            def fail(self):
                if self is db:
                    raise RuntimeError("commit failed before send")
                return original(self)

            monkeypatch.setattr(Session, "commit", fail)
            with pytest.raises(RuntimeError, match="before send"):
                publish_scores(
                    db, batch, ScorePayload(), storage_format=StorageFormat.CURRENT
                )
            assert read_payload(db, handle) == payload()
    finally:
        engine.dispose()


def test_ambiguous_commit_recovers_on_new_connection(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'scores.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            batch = candidate(db)
            original = Session.commit

            def lose_ack(self):
                original(self)
                if self is db:
                    raise RuntimeError("commit acknowledgement lost")

            monkeypatch.setattr(Session, "commit", lose_ack)
            result = publish_scores(
                db, batch, payload(), storage_format=StorageFormat.CURRENT
            )
            assert result.generation == batch.generation
            assert load(db, result) == payload()
            assert len(list(db.scalars(select(OpeningScoreBatch)))) == 1
    finally:
        engine.dispose()


def test_superseded_result_contract():
    from app.opening_cache import OpeningScoreRecomputeResult, RecomputeDisposition
    from app.opening_rootcalc import RowIsolationTelemetry

    with pytest.raises(ValueError, match="existing batch"):
        OpeningScoreRecomputeResult("superseded", None)
    with pytest.raises(ValueError, match="no rebuild reason"):
        OpeningScoreRecomputeResult("superseded", object(), reason="cache_miss")
    with pytest.raises(ValueError, match="no row isolation"):
        OpeningScoreRecomputeResult(
            "superseded", object(), row_isolation=RowIsolationTelemetry().snapshot()
        )
    assert (
        OpeningScoreRecomputeResult("superseded", object()).disposition
        is RecomputeDisposition.SUPERSEDED
    )


def test_higher_generation_with_older_evidence_stays_stale(db_session):
    from app.opening_cache import _cheap_evidence_fresh, bump_evidence_seq

    batch = publish(db_session)
    handle = ScoreHandle.from_batch(batch)
    # A generation bump is not an evidence proof, including on superseded reload.
    for _ in range(9):
        bump_evidence_seq(db_session, 123, "white")
    db_session.commit()
    assert handle_is_live(db_session, handle)
    assert not _cheap_evidence_fresh(db_session, batch)


def test_transport_is_bounded_and_confidence_updates_only_that_field(db_session):
    value = payload()
    value = replace(
        value,
        positions=tuple(
            {**value.positions[0], "normalized_fen": f"fen-{i}"} for i in range(1001)
        ),
    )
    writes = []

    def record(conn, cursor, statement, parameters, context, many):
        if statement.startswith(
            ("INSERT INTO opening_current", "UPDATE opening_current")
        ):
            writes.append((statement, len(parameters) if many else 1))

    event.listen(db_session.get_bind(), "before_cursor_execute", record)
    try:
        publish(db_session, value)
        assert all(count <= 500 for _, count in writes)
        assert all("RETURNING" not in statement for statement, _ in writes)
        writes.clear()
        changed = replace(
            value, positions=tuple({**r, "confidence": 0.3} for r in value.positions)
        )
        publish(db_session, changed)
        assert len(writes) == 3
        assert all("SET confidence=" in statement for statement, _ in writes)
        assert all("opening_score=" not in statement for statement, _ in writes)
        assert [count for _, count in writes] == [500, 500, 1]
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", record)


def test_legacy_prune_cannot_remove_current_marker(db_session):
    from app.opening_cache import prune_old_opening_score_batches

    current = publish(db_session)
    handle = ScoreHandle.from_batch(current)
    db_session.rollback()
    assert prune_old_opening_score_batches(db_session, 123, "white", keep=0) == 0
    assert read_payload(db_session, handle) == payload()


def test_direct_write_benchmark_reports_superseded(monkeypatch):
    import app.opening_cache as cache
    from scripts.calibrate_opening_scores_v2 import run_write_bench

    winner = OpeningScoreBatch(id=17, generation=3)

    def superseded(*args, **kwargs):
        raise PublicationSuperseded(winner)

    monkeypatch.setattr(cache, "recompute_opening_scores", superseded)
    monkeypatch.setattr(cache, "list_cached_opening_scores", lambda *args: (winner, []))
    result = run_write_bench(None, 123, "white", "synthetic")
    assert result["disposition"] == "superseded"
    assert result["batch_id"] == 17


def test_ambiguous_commit_reports_a_later_winner(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'scores.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            batch = candidate(db)
            reserved = batch.generation
            original = Session.commit

            def publish_after_ack_loss(self):
                original(self)
                if self is db:
                    with Session(engine) as other:
                        publish(other, ScorePayload())
                    raise RuntimeError("ack lost after newer publication")

            monkeypatch.setattr(Session, "commit", publish_after_ack_loss)
            with pytest.raises(PublicationSuperseded) as caught:
                publish_scores(
                    db, batch, payload(), storage_format=StorageFormat.CURRENT
                )
            assert caught.value.batch.generation > reserved
            assert load(db, caught.value.batch) == ScorePayload()
    finally:
        engine.dispose()


def test_failed_reverse_conversion_keeps_complete_current_snapshot(db_session):
    current = publish(db_session)
    handle = ScoreHandle.from_batch(current)

    def fail(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO user_opening_scores"):
            raise RuntimeError("reverse conversion interrupted")

    event.listen(db_session.get_bind(), "before_cursor_execute", fail)
    try:
        db_session.rollback()
        with pytest.raises(RuntimeError, match="interrupted"):
            convert_pair(db_session, 123, "white", StorageFormat.LEGACY)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", fail)
    assert read_payload(db_session, handle) == payload()


@pytest.mark.parametrize("operation", ["prune", "convert"])
def test_maintenance_refuses_caller_transaction_without_discarding_work(db_session, operation):
    from app.opening_cache import prune_old_opening_score_batches

    pending = OpeningScoreBatch(user_id=456, player_color="black", generation=1)
    db_session.add(pending)
    with pytest.raises(ValueError, match="fresh transaction"):
        if operation == "prune":
            prune_old_opening_score_batches(db_session, 123, "white")
        else:
            convert_pair(db_session, 123, "white", StorageFormat.CURRENT)
    assert pending in db_session.new
    db_session.commit()
    assert db_session.get(OpeningScoreBatch, pending.id) is pending


@pytest.mark.parametrize("existing", [False, True])
def test_noop_conversion_does_not_reserve_generation(db_session, existing):
    from app.models import OpeningScoreCursor

    if existing:
        publish(db_session, format=StorageFormat.LEGACY)
        db_session.rollback()
    result = convert_pair(db_session, 123, "white", StorageFormat.LEGACY)
    assert (result is not None) == existing
    assert not db_session.in_transaction()
    cursor = db_session.get(OpeningScoreCursor, (123, "white"))
    assert (cursor.latest_generation if cursor else 0) == int(existing)


def test_converted_pair_cannot_be_served_or_reported_cached_by_legacy_readers(db_session):
    from app.opening_cache import list_cached_opening_scores, recompute_opening_scores_if_needed
    from app.opening_score_storage import UnsupportedScoreStorage

    publish(db_session, format=StorageFormat.LEGACY)
    assert len(list_cached_opening_scores(db_session, 123, "white")[1]) == 1
    db_session.rollback()
    convert_pair(db_session, 123, "white", StorageFormat.CURRENT)
    for reader in (list_cached_opening_scores, recompute_opening_scores_if_needed):
        with pytest.raises(UnsupportedScoreStorage, match="reverse-convert"):
            reader(db_session, 123, "white")
    db_session.rollback()
    convert_pair(db_session, 123, "white", StorageFormat.LEGACY)
    assert len(list_cached_opening_scores(db_session, 123, "white")[1]) == 1


def test_legacy_bulk_inserts_keep_dialect_paging(db_session):
    value = payload()
    value = replace(value, positions=tuple(
        {**value.positions[0], "normalized_fen": f"fen-{i}"} for i in range(1001)
    ))
    writes = []

    def record(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO opening_position_scores"):
            writes.append(len(parameters))

    event.listen(db_session.get_bind(), "before_cursor_execute", record)
    try:
        publish(db_session, value, format=StorageFormat.LEGACY)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", record)
    assert writes == [1001]


@pytest.mark.parametrize("owner,color", [(True, "white"), (123, "invalid")])
def test_noop_conversion_still_validates_pair(db_session, owner, color):
    with pytest.raises(ValueError):
        convert_pair(db_session, owner, color, StorageFormat.LEGACY)
    assert not db_session.in_transaction()
