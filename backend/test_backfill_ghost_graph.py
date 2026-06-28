import contextlib
import re
import uuid

import chess
from sqlalchemy import event

from app.api.session import _upsert_session_position_graph
from app.fen import fen_hash, normalize_fen
from app.models import Move, Position, SessionMove
from scripts.backfill_ghost_graph import _MoveRow, backfill_ghost_graph

FEN_START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
FEN_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
FEN_AFTER_E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


def _move_row(fen_before: str, san: str) -> _MoveRow:
    """Build a coherent _MoveRow by playing `san` from `fen_before`."""
    board = chess.Board(fen_before)
    board.push(board.parse_san(san))
    return _MoveRow(fen_before=fen_before, move_san=san, fen_after=board.fen())


def _linear_game_moves(count: int) -> list[_MoveRow]:
    """Build `count` plies of a transposition-free game from the start position.

    Each ply is a coherent _MoveRow (fen_before/move_san/fen_after) and no
    normalized position repeats, so positions and edges both grow linearly with
    `count` — the input shape the O(1)-round-trip perf guard needs.
    """
    board = chess.Board()
    seen = {normalize_fen(board.fen())}
    rows: list[_MoveRow] = []
    while len(rows) < count:
        fen_before = board.fen()
        chosen = None
        for mv in board.legal_moves:
            board.push(mv)
            is_new = normalize_fen(board.fen()) not in seen
            board.pop()
            if is_new:
                chosen = mv
                break
        if chosen is None:
            raise AssertionError(f"no non-transposing move after {len(rows)} plies")
        san = board.san(chosen)
        board.push(chosen)
        seen.add(normalize_fen(board.fen()))
        rows.append(_MoveRow(fen_before=fen_before, move_san=san, fen_after=board.fen()))
    return rows


class _PositionMoveSelectCounter:
    r"""Count SELECT statements that read the ``positions``/``moves`` tables.

    The g-wlzj perf guard asserts _upsert_session_position_graph resolves all
    positions and edges with a FIXED number of bulk SELECTs (one positions lookup
    + one moves lookup), independent of move count. We classify by the leading
    verb so the Phase 3 ``INSERT INTO positions`` and Phase 6 ``INSERT INTO
    moves`` flushes are NOT tallied — only the read round-trips the rewrite bounds.

    ``\bmoves\b`` matches the standalone ``moves`` table but not ``session_moves``
    (``_`` is a word character, so no word boundary precedes its "moves").
    """

    _TABLES = re.compile(r"\bpositions\b|\bmoves\b")

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if not lowered.lstrip().startswith("select"):
            return
        if self._TABLES.search(lowered):
            self.count += 1


@contextlib.contextmanager
def _count_position_move_selects(db_session):
    counter = _PositionMoveSelectCounter()
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", counter)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", counter)


def _seed_move(db, session_id, *, move_number, color, move_san, fen_before, fen_after):
    db.add(
        SessionMove(
            session_id=uuid.UUID(session_id),
            move_number=move_number,
            color=color,
            move_san=move_san,
            fen_before=fen_before,
            fen_after=fen_after,
        )
    )


def _position(db, user_id, fen):
    return (
        db.query(Position)
        .filter(Position.user_id == user_id, Position.fen_hash == fen_hash(fen))
        .first()
    )


def test_backfill_happy_path(create_game_session, db_session):
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    _seed_move(db_session, session_id, move_number=1, color="white", move_san="e4",
               fen_before=FEN_START, fen_after=FEN_AFTER_E4)
    _seed_move(db_session, session_id, move_number=1, color="black", move_san="e5",
               fen_before=FEN_AFTER_E4, fen_after=FEN_AFTER_E5)
    db_session.commit()

    stats = backfill_ghost_graph(db_session, progress_every=0)

    assert stats.valid_moves == 2
    assert stats.edges_created == 2
    start_pos = _position(db_session, user_id, FEN_START)
    e4_pos = _position(db_session, user_id, FEN_AFTER_E4)
    assert start_pos is not None and e4_pos is not None
    assert _position(db_session, user_id, FEN_AFTER_E5) is not None
    edge = (
        db_session.query(Move)
        .filter(Move.from_position_id == start_pos.id, Move.move_san == "e4")
        .first()
    )
    assert edge is not None and edge.to_position_id == e4_pos.id


def test_backfill_skips_incoherent_move(create_game_session, db_session):
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    # c5 does not transform FEN_AFTER_E4 into FEN_AFTER_E5 (which came from e5).
    _seed_move(db_session, session_id, move_number=1, color="black", move_san="c5",
               fen_before=FEN_AFTER_E4, fen_after=FEN_AFTER_E5)
    db_session.commit()

    stats = backfill_ghost_graph(db_session, progress_every=0)

    assert stats.invalid_moves == 1
    assert stats.edges_created == 0
    assert db_session.query(Move).count() == 0


def test_backfill_idempotent(create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    _seed_move(db_session, session_id, move_number=1, color="white", move_san="e4",
               fen_before=FEN_START, fen_after=FEN_AFTER_E4)
    db_session.commit()

    backfill_ghost_graph(db_session, progress_every=0)
    positions_after_first = db_session.query(Position).count()
    moves_after_first = db_session.query(Move).count()

    second = backfill_ghost_graph(db_session, progress_every=0)

    assert second.positions_created == 0
    assert second.edges_created == 0
    assert second.edges_existing == 1
    assert db_session.query(Position).count() == positions_after_first
    assert db_session.query(Move).count() == moves_after_first


def test_backfill_dry_run_commits_nothing(create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    _seed_move(db_session, session_id, move_number=1, color="white", move_san="e4",
               fen_before=FEN_START, fen_after=FEN_AFTER_E4)
    db_session.commit()

    stats = backfill_ghost_graph(db_session, progress_every=0, dry_run=True)

    assert stats.edges_created == 1
    assert db_session.query(Move).count() == 0
    assert db_session.query(Position).count() == 0


def test_backfill_user_scoped_positions(create_game_session, db_session):
    session_a = create_game_session(user_id=111, player_color="white")
    session_b = create_game_session(user_id=222, player_color="white")
    for sid in (session_a, session_b):
        _seed_move(db_session, sid, move_number=1, color="white", move_san="e4",
                   fen_before=FEN_START, fen_after=FEN_AFTER_E4)
    db_session.commit()

    backfill_ghost_graph(db_session, progress_every=0)

    pos_a = _position(db_session, 111, FEN_START)
    pos_b = _position(db_session, 222, FEN_START)
    assert pos_a is not None and pos_b is not None
    assert pos_a.id != pos_b.id


def test_upsert_uses_constant_select_round_trips(db_session):
    """O(1) bulk lookups: SELECT count is fixed regardless of move count (g-wlzj).

    Exactly one positions SELECT (Phase 2) + one moves SELECT (Phase 4); the
    Phase 3/6 INSERT flushes are not SELECTs and so are not counted. Distinct
    users keep each call's positions fresh so dedup never elides a lookup.
    """
    with _count_position_move_selects(db_session) as counter:
        _upsert_session_position_graph(db_session, user_id=123, moves=_linear_game_moves(2))
    db_session.commit()
    assert counter.count == 2

    with _count_position_move_selects(db_session) as counter:
        _upsert_session_position_graph(db_session, user_id=456, moves=_linear_game_moves(10))
    db_session.commit()
    assert counter.count == 2

    with _count_position_move_selects(db_session) as counter:
        _upsert_session_position_graph(db_session, user_id=789, moves=[])
    assert counter.count == 0


def test_upsert_in_batch_duplicate_edge_counted_existing(db_session):
    """The same (fen_before, move_san) twice in one call: created once, existing once."""
    edge = _move_row(FEN_START, "e4")
    stats = _upsert_session_position_graph(db_session, user_id=123, moves=[edge, edge])
    db_session.commit()

    assert stats.valid_moves == 2
    assert stats.edges_created == 1
    assert stats.edges_existing == 1
    assert db_session.query(Move).filter(Move.move_san == "e4").count() == 1


def test_upsert_preexisting_edge_not_duplicated(db_session):
    """A committed (from_id, move_san) edge re-uploaded counts existing, no dup row."""
    user_id = 123
    first = _upsert_session_position_graph(
        db_session, user_id=user_id, moves=[_move_row(FEN_START, "e4")]
    )
    db_session.commit()
    assert first.edges_created == 1

    second = _upsert_session_position_graph(
        db_session, user_id=user_id, moves=[_move_row(FEN_START, "e4")]
    )
    db_session.commit()

    assert second.edges_created == 0
    assert second.edges_existing == 1
    assert db_session.query(Move).filter(Move.move_san == "e4").count() == 1


def test_upsert_first_seen_fen_raw_wins_on_hash_equal_fens(db_session):
    """Two raw FENs that normalize to one hash: one Position, first-seen raw kept."""
    user_id = 123
    fen_first = FEN_AFTER_E4
    # Same position, different halfmove/fullmove clocks (normalize_fen strips them).
    fen_variant = FEN_AFTER_E4.rsplit(" ", 2)[0] + " 7 9"
    assert fen_hash(fen_first) == fen_hash(fen_variant)
    assert fen_first != fen_variant

    moves = [_move_row(fen_first, "e5"), _move_row(fen_variant, "Nf6")]
    stats = _upsert_session_position_graph(db_session, user_id=user_id, moves=moves)
    db_session.commit()

    positions = (
        db_session.query(Position)
        .filter(Position.user_id == user_id, Position.fen_hash == fen_hash(fen_first))
        .all()
    )
    assert len(positions) == 1
    assert positions[0].fen_raw == fen_first
    assert stats.edges_created == 2
