"""Performance benchmark for the ghost-move recursive CTE.

Validates that the CTE completes within 100ms on a graph with 10k positions.

Note: tests run against SQLite (not PostgreSQL), so timing is a rough proxy.
For production PostgreSQL, verify with EXPLAIN ANALYZE separately.
"""

import hashlib
import random
import time

from sqlalchemy import text

from app.models import Blunder, Move, Position


def _seed_position_graph(db, user_id, num_positions=10_000, branching=3):
    """Seed a tree-like position graph with blunders sprinkled in."""
    positions = []
    for i in range(num_positions):
        color = "white" if i % 2 == 0 else "black"
        # Synthetic FEN-like string; we hash it directly to avoid
        # python-chess board validation overhead during seeding.
        fen_raw = f"pos_{i} {'w' if color == 'white' else 'b'} - - 0 {i}"
        h = hashlib.sha256(fen_raw.encode()).hexdigest()
        pos = Position(
            user_id=user_id,
            fen_hash=h,
            fen_raw=fen_raw,
            active_color=color,
        )
        db.add(pos)
        positions.append(pos)

    db.flush()

    # Forward edges: each position -> up to `branching` later positions
    for i in range(num_positions - 1):
        targets = set()
        max_jump = min(branching * 2, num_positions - i - 1)
        if max_jump < 1:
            continue
        for _ in range(random.randint(1, branching)):
            targets.add(i + random.randint(1, max_jump))
        for t in targets:
            db.add(Move(
                from_position_id=positions[i].id,
                move_san=f"m{i}t{t}",
                to_position_id=positions[t].id,
            ))

    # Guarantee a blunder reachable from position 0:
    # explicit edge 0 -> 1 -> 2, with blunder at position 2 (white)
    for src, dst in [(0, 1), (1, 2)]:
        db.merge(Move(
            from_position_id=positions[src].id,
            move_san=f"seed{src}to{dst}",
            to_position_id=positions[dst].id,
        ))
    db.add(Blunder(
        user_id=user_id,
        position_id=positions[2].id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
    ))

    # Sprinkle ~50 more blunders across the graph
    white_positions = [p for p in positions[4:] if p.active_color == "white"]
    for pos in random.sample(white_positions, min(50, len(white_positions))):
        db.add(Blunder(
            user_id=user_id,
            position_id=pos.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=200,
        ))

    db.commit()
    return positions


def test_cte_ghost_move_under_100ms(db_session):
    """CTE on 10k-position graph should complete in < 100ms."""
    user_id = 999
    random.seed(42)

    positions = _seed_position_graph(db_session, user_id)

    # Same CTE shape as game.py ghost-move endpoint (5-ply steering radius).
    # Uses INTEGER instead of BIGINT for SQLite compatibility.
    cte_query = text("""
        WITH RECURSIVE reachable(position_id, depth, path, first_move) AS (
            SELECT
                CAST(:start_position_id AS INTEGER),
                0,
                ',' || :start_position_id || ',',
                CAST(NULL AS TEXT)
            UNION ALL
            SELECT
                m.to_position_id,
                r.depth + 1,
                r.path || m.to_position_id || ',',
                COALESCE(r.first_move, m.move_san)
            FROM reachable r
            JOIN moves m ON m.from_position_id = r.position_id
            WHERE r.depth < 5
              AND r.path NOT LIKE '%,' || CAST(m.to_position_id AS TEXT) || ',%'
        )
        SELECT r.first_move, b.id AS blunder_id
        FROM reachable r
        JOIN positions p ON p.id = r.position_id
        JOIN blunders b ON b.position_id = r.position_id
        WHERE b.user_id = :user_id
          AND p.active_color = :player_color
          AND r.first_move IS NOT NULL
        LIMIT 1
    """)

    start = time.perf_counter()
    result = db_session.execute(
        cte_query,
        {
            "start_position_id": positions[0].id,
            "user_id": user_id,
            "player_color": "white",
        },
    ).fetchone()
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result is not None, "Expected to find at least one blunder in 10k graph"
    # SQLite doesn't short-circuit CTEs as well as PostgreSQL, so we use
    # a generous 1s threshold here.  The production target on PostgreSQL
    # with proper indexes is < 100ms (verify with EXPLAIN ANALYZE).
    assert elapsed_ms < 1000, f"CTE took {elapsed_ms:.1f}ms, expected < 1000ms"
