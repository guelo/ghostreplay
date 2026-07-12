from __future__ import annotations

import ast
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import Blunder, GameSession, Position
from app.row_locks import for_no_key_update
from conftest import pg_required


API_MODULES = ("session.py", "game.py", "drills.py", "blunder.py", "srs.py")
LOCK_MODULES = API_MODULES + ("row_locks.py",)
REQUIRED_DIRECT_LOCKS = {
    "session.py": {"upsert_session_moves": "for_no_key_update"},
    # end_game locks at entry; get_next_opponent_move locks only the active
    # pre-root drill branch it mutates (g-branch-locks).
    "game.py": {
        "end_game": "for_no_key_update",
        "get_next_opponent_move": "for_no_key_update",
    },
    "blunder.py": {"record_blunder": "for_no_key_update"},
    "srs.py": {"review_blunder": "for_no_key_update"},
    "drills.py": {"_get_drill_for_update": "for_no_key_update"},
}
REQUIRED_DRILL_WRITER_LOCKS = {
    "fail_drill",
    "continue_drill",
    "natural_end_drill",
    "abandon_drill",
    # check_drill_route locks only the two mutating branches (g-branch-locks); its
    # root-reached and on-route snapshot responses stay unlocked.
    "check_drill_route",
}


def _compiled_lock(db: Session, model: type) -> str:
    query = for_no_key_update(db.query(model).filter(model.id.is_not(None)))
    return str(
        query.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_sanctioned_session_and_blunder_locks_compile_as_nku(db_session):
    for model in (GameSession, Blunder):
        sql = _compiled_lock(db_session, model)
        assert "FOR NO KEY UPDATE" in sql
        assert not sql.rstrip().endswith("FOR UPDATE")


def _function_calls(tree: ast.AST, function_name: str) -> set[str]:
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    calls: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def test_required_writer_routes_acquire_sanctioned_locks():
    api_dir = Path(__file__).parent / "app" / "api"
    for module_name, required in REQUIRED_DIRECT_LOCKS.items():
        tree = ast.parse((api_dir / module_name).read_text())
        for function_name, lock_call in required.items():
            assert lock_call in _function_calls(tree, function_name), (
                f"{module_name}:{function_name} must call {lock_call}"
            )

    drills_tree = ast.parse((api_dir / "drills.py").read_text())
    for function_name in REQUIRED_DRILL_WRITER_LOCKS:
        assert "_get_drill_for_update" in _function_calls(
            drills_tree, function_name
        ), f"drills.py:{function_name} must acquire the drill writer lock"


def test_lock_modules_reject_non_nku_with_for_update_shapes():
    app_dir = Path(__file__).parent / "app"
    violations: list[str] = []
    for name in LOCK_MODULES:
        path = app_dir / name if name == "row_locks.py" else app_dir / "api" / name
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "with_for_update":
                continue
            key_share = next(
                (kw.value for kw in node.keywords if kw.arg == "key_share"), None
            )
            read = next((kw.value for kw in node.keywords if kw.arg == "read"), None)
            key_share_is_true = (
                isinstance(key_share, ast.Constant) and key_share.value is True
            )
            read_is_safe = read is None or (
                isinstance(read, ast.Constant) and read.value is False
            )
            if not key_share_is_true or not read_is_safe:
                violations.append(f"{name}:{node.lineno}")
    assert violations == []


@pytest.mark.parametrize(
    ("session_lock", "blunder_lock", "expect_deadlock"),
    [
        ("FOR UPDATE", "FOR UPDATE", True),
        ("FOR UPDATE", "FOR NO KEY UPDATE", False),
        ("FOR NO KEY UPDATE", "FOR UPDATE", False),
        ("FOR NO KEY UPDATE", "FOR NO KEY UPDATE", False),
    ],
    ids=[
        "both_for_update",
        "session_fu_blunder_nku",
        "session_nku_blunder_fu",
        "both_nku",
    ],
)
@pg_required
def test_srs_moves_cross_root_lock_matrix(
    pg_session_factory, session_lock, blunder_lock, expect_deadlock
):
    seed = pg_session_factory()
    try:
        user_id = 123
        game = GameSession(
            id=uuid.uuid4(),
            user_id=user_id,
            started_at=datetime.now(timezone.utc),
            status="active",
            engine_elo=1500,
            player_color="white",
        )
        position = Position(
            user_id=user_id,
            fen_hash=f"writer-lock-{uuid.uuid4()}",
            fen_raw="8/8/8/8/8/8/8/8 w - - 0 1",
            active_color="white",
        )
        seed.add_all([game, position])
        seed.flush()
        blunder = Blunder(
            user_id=user_id,
            position_id=position.id,
            bad_move_san="Qh5",
            best_move_san="Nf3",
            eval_loss_cp=100,
        )
        seed.add(blunder)
        seed.commit()
        session_id, blunder_id = game.id, blunder.id
    finally:
        seed.close()

    barrier = threading.Barrier(2)

    def run(root: str) -> str:
        db = pg_session_factory()
        try:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            if root == "session":
                db.execute(
                    text(f"SELECT id FROM game_sessions WHERE id = :id {session_lock}"),
                    {"id": session_id},
                )
                barrier.wait(timeout=5)
                db.execute(
                    text("SELECT id FROM blunders WHERE id = :id FOR KEY SHARE"),
                    {"id": blunder_id},
                )
            else:
                db.execute(
                    text(f"SELECT id FROM blunders WHERE id = :id {blunder_lock}"),
                    {"id": blunder_id},
                )
                barrier.wait(timeout=5)
                db.execute(
                    text("SELECT id FROM game_sessions WHERE id = :id FOR KEY SHARE"),
                    {"id": session_id},
                )
            db.commit()
            return "committed"
        except OperationalError as exc:
            db.rollback()
            sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
                exc.orig, "pgcode", None
            )
            if sqlstate == "40P01":
                return "deadlocked"
            raise
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=10)
            for future in (pool.submit(run, "session"), pool.submit(run, "blunder"))
        ]

    assert outcomes.count("deadlocked") == (1 if expect_deadlock else 0)
    assert outcomes.count("committed") == (1 if expect_deadlock else 2)
