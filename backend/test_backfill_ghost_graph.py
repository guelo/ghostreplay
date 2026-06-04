import uuid

from app.fen import fen_hash
from app.models import Move, Position, SessionMove
from scripts.backfill_ghost_graph import backfill_ghost_graph

FEN_START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
FEN_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
FEN_AFTER_E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


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
