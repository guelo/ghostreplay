"""PostgreSQL proof that the replay-cache digest's two formatters agree
(g-overlay-evidence-reuse).

``_build_move_rows`` validates its per-session replay cache against a digest the
DATABASE computes (``_probe_sql``), and stores entries under a digest PYTHON
computes from the rows it fetched (``_session_digest_body`` + the dialect's
``_body_fold``). If those two ever disagree, nothing is WRONG — every build
simply re-replays the user's whole history, silently turning a ~0.3 s warm
rebuild back into a ~12 s one. The default suite runs on SQLite, so a
Postgres-only formatting divergence (a cast rendering, a collation-dependent
ordering, NULL handling in the concatenation) would ship completely unseen.

PostgreSQL is also the one dialect that FOLDS the body server-side, with md5 —
SQLite has no built-in hash, so on SQLite the fold is the identity on both sides
and proves nothing. Two claims therefore live only here: that the raw
aggregate is byte-equal to python's, and that the md5 pair agrees on top of it.
The fold is what keeps the probe's payload O(sessions) instead of O(evidence
bytes), so ``test_probe_payload_is_fixed_size_per_session`` guards the
performance claim itself, not just correctness.

These tests skip cleanly without ``GHOSTREPLAY_TEST_PG_URL``. Everything
provable on SQLite is already covered by ``TestReplayDigestColumnCoverage`` and
``TestProbePayloadFold`` in ``test_opening_evidence.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import chess
import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

import app.opening_evidence as opening_evidence
from conftest import pg_required

USER_ID = 4242
COLOR = "white"


def _seed(db, *, sessions: int = 3, plies: int = 12) -> list[str]:
    """Ended sessions whose rows cover every digest-relevant shape: both colors at
    the same move number, NULL and non-NULL evals, and negative eval values."""
    db.execute(
        text(
            "INSERT INTO users (id, username, is_anonymous) VALUES (:id, :u, true)"
            " ON CONFLICT (id) DO NOTHING"
        ),
        {"id": USER_ID, "u": f"digest{USER_ID}"},
    )
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    sids: list[str] = []
    for index in range(sessions):
        sid = str(uuid.uuid4())
        sids.append(sid)
        db.execute(
            text(
                "INSERT INTO game_sessions (id, user_id, started_at, ended_at,"
                " status, engine_elo, player_color, is_rated, session_mode)"
                " VALUES (:id, :uid, :sa, :ea, 'ended', 1500, :pc, true, 'normal')"
            ),
            {
                "id": sid,
                "uid": USER_ID,
                "sa": base + timedelta(minutes=index),
                "ea": base + timedelta(minutes=index, seconds=30),
                "pc": COLOR,
            },
        )
        board = chess.Board()
        for ply in range(plies):
            moves = sorted(m.uci() for m in board.legal_moves)
            if not moves:
                break
            uci = moves[(index + ply) % len(moves)]
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            fen_before = board.fen()
            color = "white" if board.turn == chess.WHITE else "black"
            board.push(move)
            # ply % 3 == 0 leaves BOTH eval columns NULL, so the coalesce sentinel
            # is exercised; the negative branch pins integer text rendering.
            has_primary = (ply % 3) != 0
            db.execute(
                text(
                    "INSERT INTO session_moves (session_id, move_number, color,"
                    " move_san, fen_before, fen_after, eval_delta, eval_cp,"
                    " best_move_eval_cp, segment)"
                    " VALUES (:sid, :mn, :c, :ms, :fb, :fa, :ed, :ec, :bc, 'normal')"
                ),
                {
                    "sid": sid,
                    "mn": ply // 2 + 1,
                    "c": color,
                    "ms": san,
                    "fb": fen_before,
                    "fa": board.fen(),
                    "ed": -25 if ply % 2 else 40,
                    "ec": -70 if has_primary else None,
                    "bc": 55 if has_primary else None,
                },
            )
    db.commit()
    return sids


def _probe(db) -> dict:
    """The production probe: md5-folded body."""
    rows = db.execute(
        text(opening_evidence._probe_sql("postgresql")),
        {"user_id": USER_ID, "player_color": COLOR},
    ).fetchall()
    return {str(r.sid): r for r in rows}


def _probe_unfolded(db) -> dict:
    """The same statement with the IDENTITY fold, so the raw aggregate can be
    inspected on PostgreSQL. Built from ``_probe_sql`` rather than hand-written so
    the filters, GROUP BY and explicit color rank cannot drift from production;
    only the fold differs (asserted in ``TestProbePayloadFold``)."""
    rows = db.execute(
        text(opening_evidence._probe_sql("sqlite")),
        {"user_id": USER_ID, "player_color": COLOR},
    ).fetchall()
    return {str(r.sid): r for r in rows}


def _fetch(db, sids: list[str]) -> dict[str, list]:
    rows = db.execute(
        text(opening_evidence._SESSION_ROWS_SQL).bindparams(
            bindparam("sids", expanding=True)
        ),
        {"user_id": USER_ID, "player_color": COLOR, "sids": sids},
    ).fetchall()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(row.session_id), []).append(row)
    return grouped


@pg_required
def test_sql_and_python_digest_bodies_are_byte_equal(pg_session_factory):
    """The raw aggregate, before folding. This is the assertion that catches a
    cast rendering or NULL-handling divergence; the md5 on top would hide WHERE
    the difference is, so it is checked separately below."""
    db = pg_session_factory()
    try:
        sids = _seed(db)
        raw = _probe_unfolded(db)
        assert set(raw) == set(sids)
        fetched = _fetch(db, sids)

        for sid in sids:
            rows = fetched[sid]
            assert opening_evidence._session_digest_body(rows) == raw[sid].body, (
                f"PostgreSQL and python digest bodies diverge for session {sid} — "
                "every overlay build would re-replay the whole user history"
            )
            assert len(rows) == raw[sid].row_count
    finally:
        db.close()


@pg_required
def test_md5_fold_pair_agrees_and_keys_match_end_to_end(pg_session_factory):
    """PostgreSQL's ``md5()`` and python's ``hashlib.md5`` must produce the same
    text for the same body, and the resulting cache KEYS must be equal — the
    probe's key and the key a build would store are then interchangeable."""
    db = pg_session_factory()
    try:
        sids = _seed(db)
        probe = _probe(db)
        raw = _probe_unfolded(db)
        fetched = _fetch(db, sids)
        fold = opening_evidence._body_fold("postgresql")[1]

        for sid in sids:
            rows = fetched[sid]
            assert fold(raw[sid].body) == probe[sid].body, (
                "PostgreSQL md5() and hashlib.md5 disagree"
            )
            assert opening_evidence._session_digest(
                len(rows),
                fold(opening_evidence._session_digest_body(rows)),
                rows[0].session_ts,
                fold(opening_evidence._session_pgn_body(rows[0].session_pgn)),
                rows[0].terminal_line_reconciled,
            ) == opening_evidence._session_digest(
                probe[sid].row_count,
                probe[sid].body,
                probe[sid].session_ts,
                probe[sid].session_pgn_body,
                probe[sid].terminal_line_reconciled,
            )
    finally:
        db.close()


@pg_required
def test_probe_payload_is_fixed_size_per_session(pg_session_factory):
    """The performance claim, asserted rather than inferred: the probe's payload
    is O(sessions), not O(evidence bytes). A 4-ply and a 60-ply session must
    return the SAME number of body bytes, so a warm build's wire cost does not
    scale with history and the measured figure is not a loopback artefact."""
    db = pg_session_factory()
    try:
        short = _seed(db, sessions=1, plies=4)[0]
        long_ = _seed(db, sessions=1, plies=60)[0]
        probe = _probe(db)
        raw = _probe_unfolded(db)

        assert len(probe[short].body) == 32
        assert len(probe[long_].body) == 32
        assert len(probe[short].session_pgn_body) == 32
        assert len(probe[long_].session_pgn_body) == 32
        # The unfolded bodies really do differ in size, or the claim is vacuous.
        assert probe[long_].row_count >= 3 * probe[short].row_count
        assert len(raw[long_].body) >= 3 * len(raw[short].body)
    finally:
        db.close()


@pg_required
def test_raw_digest_folds_pgn_once_via_postgres_uuid_keys(pg_session_factory):
    """The separate one-row-per-session PGN query must preserve UUID typing."""
    db = pg_session_factory()
    try:
        sid = _seed(db, sessions=1, plies=12)[0]
        before = opening_evidence.raw_evidence_inputs_digest(db, USER_ID, COLOR)
        db.execute(
            text(
                "UPDATE game_sessions SET pgn='1. e4 *', "
                "terminal_line_reconciled=true WHERE id=:sid"
            ),
            {"sid": sid},
        )
        db.commit()

        after = opening_evidence.raw_evidence_inputs_digest(db, USER_ID, COLOR)
        assert after != before
    finally:
        db.close()


@pg_required
def test_probe_ordering_is_collation_independent(pg_session_factory):
    """The ORDER BY uses an explicit white/black rank, not ``ORDER BY sm.color``,
    so the digest cannot shift with the database's collation. Assert the body's row
    order really is move_number-then-white-then-black."""
    db = pg_session_factory()
    try:
        sids = _seed(db, sessions=1, plies=12)
        raw = _probe_unfolded(db)
        body = raw[sids[0]].body
        rows = _fetch(db, sids)[sids[0]]

        seen = [
            (int(line.split("|")[0]), line.split("|")[1])
            for line in body.split(opening_evidence._DIGEST_ROW_SEP)
        ]
        assert seen == sorted(
            seen, key=lambda mc: (mc[0], opening_evidence._COLOR_RANK.get(mc[1], 2))
        )
        assert len(seen) == len(rows)
        # Both colors really are present at some shared move number, or the
        # ordering claim would be vacuous.
        assert len({color for _, color in seen}) == 2
    finally:
        db.close()


@pg_required
def test_null_evals_use_the_sentinel_not_an_empty_field(pg_session_factory):
    """A NULL must render as the sentinel: were it rendered empty, NULL and '' would
    collide; were the concatenation left NULL, ``string_agg`` would DROP the row and
    hide a real change."""
    db = pg_session_factory()
    try:
        sids = _seed(db, sessions=1, plies=12)
        body = _probe_unfolded(db)[sids[0]].body
        lines = body.split(opening_evidence._DIGEST_ROW_SEP)
        rows = _fetch(db, sids)[sids[0]]

        assert len(lines) == len(rows), "a row vanished from the aggregate"
        sentinel_lines = [
            line for line in lines
            if f"|{opening_evidence._DIGEST_NULL}|" in line
            or line.endswith(f"|{opening_evidence._DIGEST_NULL}")
        ]
        assert sentinel_lines, "fixture never produced a NULL eval column"
        assert "||" not in body, "a NULL rendered as an empty field"
    finally:
        db.close()


@pg_required
def test_warm_rebuild_on_postgres_fetches_no_rows(pg_session_factory, monkeypatch):
    """End to end on PostgreSQL: after a cold build, a rebuild replays nothing and
    issues no scoped row fetch. This is the regression the byte-equality above
    exists to protect."""
    from app.opening_graph import get_opening_graph

    db = pg_session_factory()
    try:
        _seed(db, sessions=3, plies=12)
        opening_evidence.reset_session_evidence_cache()
        graph = get_opening_graph()
        opening_evidence.overlay_evidence(db, USER_ID, COLOR, graph)  # cold

        replays = {"n": 0}
        real = opening_evidence.reconstruct_board_sequence

        def counting(moves):
            replays["n"] += 1
            return real(moves)

        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counting)

        fetches = {"n": 0}
        real_execute = type(db).execute

        def counting_execute(self, stmt, *args, **kwargs):
            if "sm.session_id IN" in str(stmt):
                fetches["n"] += 1
            return real_execute(self, stmt, *args, **kwargs)

        monkeypatch.setattr(type(db), "execute", counting_execute)
        opening_evidence.overlay_evidence(db, USER_ID, COLOR, graph)  # warm

        assert replays["n"] == 0, "PostgreSQL warm rebuild re-replayed sessions"
        assert fetches["n"] == 0, "PostgreSQL warm rebuild re-fetched raw rows"
    finally:
        db.close()


@pg_required
def test_real_recompute_persists_l2_across_caller_rollback(
    pg_session_factory, monkeypatch
):
    """Production transaction shape: recompute rolls back its evidence Session
    before CPU scoring, while the cache-only transaction must survive and hydrate
    a fresh Session after a simulated process restart."""
    import app.opening_cache as opening_cache
    from app.opening_graph import get_opening_graph

    db = pg_session_factory()
    verify = None
    try:
        seeded_session_ids = _seed(db, sessions=2, plies=12)
        db.execute(
            text(
                "DELETE FROM opening_session_replay_cache "
                "WHERE session_id IN ("
                "  SELECT id FROM game_sessions "
                "  WHERE user_id = :user_id AND player_color = :color"
                ")"
            ),
            {"user_id": USER_ID, "color": COLOR},
        )
        db.commit()
        opening_evidence.reset_session_evidence_cache()

        original_username = f"digest{USER_ID}"
        uncommitted_username = f"uncommitted-{USER_ID}"
        real_overlay_evidence = opening_cache.overlay_evidence

        def overlay_with_uncommitted_sentinel(session, *args, **kwargs):
            overlay = real_overlay_evidence(session, *args, **kwargs)
            # This write happens after L2 population and immediately before
            # recompute's documented rollback. Generation reservation commits
            # earlier, so a sentinel installed before recompute would not test
            # this boundary.
            session.execute(
                text("UPDATE users SET username = :username WHERE id = :user_id"),
                {"username": uncommitted_username, "user_id": USER_ID},
            )
            assert (
                session.execute(
                    text("SELECT username FROM users WHERE id = :user_id"),
                    {"user_id": USER_ID},
                ).scalar_one()
                == uncommitted_username
            )
            return overlay

        monkeypatch.setattr(
            opening_cache,
            "overlay_evidence",
            overlay_with_uncommitted_sentinel,
        )
        opening_cache.recompute_opening_scores(db, USER_ID, COLOR)

        verify = pg_session_factory()
        # Pin the caller rollback itself: if recompute ever commits before its
        # CPU phase, this sentinel leaks and the L2-survival proof is vacuous.
        assert (
            verify.execute(
                text("SELECT username FROM users WHERE id = :user_id"),
                {"user_id": USER_ID},
            ).scalar_one()
            == original_username
        )
        persisted_rows = verify.execute(
            text(
                "SELECT CAST(cache.session_id AS TEXT) AS session_id, "
                "       cache.content_hash, cache.divider_version, "
                "       cache.inputs_version, cache.payload_version, "
                "       cache.move_count, cache.payload, cache.updated_at "
                "FROM opening_session_replay_cache cache "
                "JOIN game_sessions gs ON gs.id = cache.session_id "
                "WHERE gs.user_id = :user_id AND gs.player_color = :color"
            ),
            {"user_id": USER_ID, "color": COLOR},
        ).all()
        assert {row.session_id for row in persisted_rows} == set(
            seeded_session_ids
        )
        for row in persisted_rows:
            payload = json.loads(row.payload)
            assert len(row.content_hash) == 40
            assert row.divider_version == opening_evidence.game_phase.DIVIDER_VERSION
            assert (
                row.inputs_version
                == opening_evidence.OPENING_EVIDENCE_INPUTS_VERSION
            )
            assert (
                row.payload_version
                == opening_evidence.SESSION_REPLAY_PAYLOAD_VERSION
            )
            assert len(payload["moves"]) == row.move_count
            assert datetime.fromisoformat(payload["session_ts"]).tzinfo is not None
            assert row.updated_at.tzinfo is not None

        opening_evidence.reset_session_evidence_cache()

        def fail_reconstruction(*_args, **_kwargs):
            raise AssertionError("persisted bootstrap replayed raw boards")

        monkeypatch.setattr(
            opening_evidence,
            "reconstruct_board_sequence",
            fail_reconstruction,
        )
        overlay = opening_evidence.overlay_evidence(
            verify, USER_ID, COLOR, get_opening_graph()
        )
        assert overlay.replay_cache_stats.l2_hits == len(persisted_rows)
        assert overlay.replay_cache_stats.raw_derivations == 0

        deleted_sid = seeded_session_ids[0]
        verify.execute(
            text("DELETE FROM game_sessions WHERE id = CAST(:sid AS UUID)"),
            {"sid": deleted_sid},
        )
        verify.commit()
        assert (
            verify.execute(
                text(
                    "SELECT count(*) FROM opening_session_replay_cache "
                    "WHERE session_id = CAST(:sid AS UUID)"
                ),
                {"sid": deleted_sid},
            ).scalar_one()
            == 0
        )

        remaining_sid = seeded_session_ids[1]
        with pytest.raises(IntegrityError):
            verify.execute(
                text(
                    "UPDATE opening_session_replay_cache SET move_count = -1 "
                    "WHERE session_id = CAST(:sid AS UUID)"
                ),
                {"sid": remaining_sid},
            )
        verify.rollback()
        assert (
            verify.execute(
                text(
                    "SELECT move_count FROM opening_session_replay_cache "
                    "WHERE session_id = CAST(:sid AS UUID)"
                ),
                {"sid": remaining_sid},
            ).scalar_one()
            >= 0
        )
    finally:
        if verify is not None:
            verify.close()
        db.close()
