import ast
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import literal, select

from app.centipawn_loss import (
    CENTIPAWN_LOSS_CAP_CP,
    centipawn_loss,
    centipawn_loss_expr,
    clamp_delta_nonneg,
    round_half_up_cpl,
)
from app.models import Blunder, GameSession, Position, SessionMove, User
from conftest import pg_gate

_CPL_CAP_VECTORS = json.loads(
    (Path(__file__).parent / "tests" / "fixtures" / "cpl_cap_vectors.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _CPL_CAP_VECTORS, ids=lambda c: str(c["input"]))
def test_centipawn_loss_matches_shared_vectors(case):
    # Same fixture drives the TS evalLoss suite, so the Python cap and the TS cap are
    # pinned to one vector set (null -> None on the Python side).
    assert centipawn_loss(case["input"]) == case["expected"]


def test_centipawn_loss_none_passes_through():
    assert centipawn_loss(None) is None


def test_centipawn_loss_cap_constant_is_1000():
    assert CENTIPAWN_LOSS_CAP_CP == 1000


def test_centipawn_loss_expr_floors_and_caps(db_session):
    row = db_session.execute(
        select(
            centipawn_loss_expr(literal(None)),
            centipawn_loss_expr(literal(-5)),
            centipawn_loss_expr(literal(10)),
            centipawn_loss_expr(literal(999)),
            centipawn_loss_expr(literal(1000)),
            centipawn_loss_expr(literal(1001)),
            centipawn_loss_expr(literal(10000)),
        )
    ).one()

    assert row == (None, 0, 10, 999, 1000, 1000, 1000)


def test_clamp_delta_nonneg_floors_but_never_caps():
    # The RAW-evidence clamp for analysis_cache: floor at 0, NO upper cap.
    assert clamp_delta_nonneg(None) is None
    assert clamp_delta_nonneg(-5) == 0
    assert clamp_delta_nonneg(10) == 10
    assert clamp_delta_nonneg(1000) == 1000
    assert clamp_delta_nonneg(1001) == 1001
    assert clamp_delta_nonneg(10000) == 10000


# ---------------------------------------------------------------------------
# round_half_up_cpl (g-22t8.5): half-up, Decimal-exact aggregate rounding.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Exact halves round UP, not to even — Python's banker's round() gives 2 for
        # 2.5 and 0 for 0.5, which is exactly the frontend divergence being closed.
        (2.5, 3),
        (Decimal("2.5"), 3),
        (0.5, 1),
        (Decimal("0.5"), 1),
        (2.49, 2),
        (Decimal("2.49"), 2),
        (2.500000000000001, 3),
        (Decimal("2.500000000000001"), 3),
        (0.0, 0),
        (Decimal("0"), 0),
        (1000.0, 1000),
        (Decimal("1000"), 1000),
    ],
)
def test_round_half_up_cpl_rounds_halves_up(value, expected):
    assert round_half_up_cpl(value) == expected


def test_round_half_up_cpl_is_decimal_exact_not_float_based():
    # THE reason the helper is Decimal-based. PostgreSQL AVG(NUMERIC) hands us a
    # Decimal; a float round-trip corrupts a near-half, because
    # float(Decimal("2.4999999999999999")) is exactly 2.5 — which would round UP to
    # 3 when the true value rounds DOWN to 2.
    near_half = Decimal("2.4999999999999999")
    assert float(near_half) == 2.5  # the corruption a float() cast would introduce
    assert round_half_up_cpl(near_half) == 2


# ---------------------------------------------------------------------------
# Shared exact-half fixtures.
#
# Both shapes are seeded identically on SQLite (the behavioural tests below) and on
# PostgreSQL (the pg_gate cast guard), and both carry a schema constraint the naive
# seed violates: session_moves must be a legal ply sequence (compute_game_accuracy
# requires move_number-then-colour order), and blunders is UNIQUE(user_id,
# position_id) on BOTH dialects, so two blunders need two distinct positions.
#
# Every seeded value passes through centipawn_loss_expr unchanged (no floor, no
# 1000-cap), so each average is exactly 2.5 and every assertion isolates the
# rounding mode alone.
# ---------------------------------------------------------------------------

_EXACT_HALF_PGN = "1. e4 e5 2. Nf3 Nc6"

# (move_number, colour, SAN, eval_delta). The player is white: their two moves lose
# 2 and 3 cp, so the player-filtered average is exactly (2 + 3) / 2 == 2.5. The black
# rows are the opponent's and never enter the aggregate.
_EXACT_HALF_MOVES = (
    (1, "white", "e4", 2),
    (1, "black", "e5", 100),
    (2, "white", "Nf3", 3),
    (2, "black", "Nc6", 100),
)


def _seed_exact_half_session(session, *, user_id: int) -> uuid.UUID:
    """Ended 4-ply game whose player Avg CPL is exactly 2.5. Commits."""
    session_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session.add(
        GameSession(
            id=session_id,
            user_id=user_id,
            started_at=now,
            ended_at=now,
            status="ended",
            result="checkmate_win",
            engine_elo=1500,
            player_color="white",
            pgn=_EXACT_HALF_PGN,
            session_mode="normal",
        )
    )
    for move_number, color, move_san, eval_delta in _EXACT_HALF_MOVES:
        session.add(
            SessionMove(
                session_id=session_id,
                move_number=move_number,
                color=color,
                move_san=move_san,
                fen_after=f"fen-{move_number}{color[0]}",
                eval_delta=eval_delta,
                segment="normal",
            )
        )
    session.commit()
    return session_id


def _seed_exact_half_blunders(session, *, user_id: int) -> None:
    """Two blunders on two DISTINCT positions, eval_loss_cp 2 and 3 -> avg 2.5. Commits."""
    for tag, eval_loss_cp in (("half-a", 2), ("half-b", 3)):
        position = Position(
            user_id=user_id,
            fen_hash=f"hash-{tag}",
            fen_raw=f"fen-{tag}",
            active_color="white",
        )
        session.add(position)
        session.flush()
        session.add(
            Blunder(
                user_id=user_id,
                position_id=position.id,
                bad_move_san="Qxh7+",
                best_move_san="Re1",
                eval_loss_cp=eval_loss_cp,
                created_at=datetime.now(timezone.utc),
            )
        )
    session.commit()


# ---------------------------------------------------------------------------
# Behavioural: every rounding site, through its own endpoint. Each returned 2 under
# the old banker's int(round(...)) and must return 3 under half-up.
# ---------------------------------------------------------------------------


def test_history_average_cpl_rounds_exact_half_up(client, auth_headers, db_session):
    _seed_exact_half_session(db_session, user_id=123)

    response = client.get("/api/history", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    assert response.json()["games"][0]["summary"]["average_centipawn_loss"] == 3


def test_session_analysis_average_cpl_rounds_exact_half_up(client, auth_headers, db_session):
    session_id = _seed_exact_half_session(db_session, user_id=123)

    response = client.get(
        f"/api/session/{session_id}/analysis", headers=auth_headers(user_id=123)
    )
    assert response.status_code == 200
    assert response.json()["summary"]["average_centipawn_loss"] == 3


def test_stats_summary_avg_blunder_eval_loss_rounds_exact_half_up(
    client, auth_headers, db_session
):
    _seed_exact_half_blunders(db_session, user_id=123)

    response = client.get("/api/stats/summary", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    assert response.json()["library"]["avg_blunder_eval_loss_cp"] == 3


# ---------------------------------------------------------------------------
# PostgreSQL call-site guard — THE authoritative cast check.
#
# The default suite runs SQLite, whose AVG already returns a float: there,
# round_half_up_cpl(float(raw)) passes every behavioural test above while
# reintroducing the near-half corruption on the real dialect. This test observes the
# runtime type at the call boundary on PostgreSQL, so no aliasing or reformatting can
# fool it. The AST contract below is a cheap always-on backstop, not a proof; this is
# the guard.
# ---------------------------------------------------------------------------


@pg_gate
def test_pg_cpl_aggregates_reach_helper_as_decimal(
    pg_client, pg_session_factory, auth_headers, monkeypatch
):
    import app.api.history as history_api
    import app.api.session as session_api
    import app.api.stats as stats_api

    user_id = 123
    # auth_headers only mints the JWT here: it seeds its users row into the SQLite
    # TestingSessionLocal, which PostgreSQL cannot see. Seed PG explicitly below.
    headers = auth_headers(user_id=user_id)

    # Seed INSIDE the test body: pg_client TRUNCATEs every table during its own setup,
    # so anything written before that setup runs is wiped.
    seed = pg_session_factory()
    try:
        # Production invariant: a valid token always maps to a real users row.
        seed.add(User(id=user_id, username=None, is_anonymous=True))
        seed.commit()
        session_id = _seed_exact_half_session(seed, user_id=user_id)
        _seed_exact_half_blunders(seed, user_id=user_id)
    finally:
        seed.close()

    seen_types: dict[str, list[type]] = {"history": [], "session": [], "stats": []}

    def _spy(module_key):
        def _record(value):
            seen_types[module_key].append(type(value))
            return round_half_up_cpl(value)

        return _record

    # Each module imports the helper by name, so patch it in each module's namespace.
    monkeypatch.setattr(history_api, "round_half_up_cpl", _spy("history"))
    monkeypatch.setattr(session_api, "round_half_up_cpl", _spy("session"))
    monkeypatch.setattr(stats_api, "round_half_up_cpl", _spy("stats"))

    history = pg_client.get("/api/history", headers=headers)
    analysis = pg_client.get(f"/api/session/{session_id}/analysis", headers=headers)
    stats = pg_client.get("/api/stats/summary", headers=headers)
    assert history.status_code == 200
    assert analysis.status_code == 200
    assert stats.status_code == 200

    # The aggregate must arrive un-cast: PostgreSQL AVG(NUMERIC) -> Decimal. A float()
    # anywhere on the path from aggregate to helper fails here.
    for module_key, recorded in seen_types.items():
        assert recorded, f"{module_key} never called round_half_up_cpl"
        assert all(t is Decimal for t in recorded), f"{module_key} passed {recorded}"

    # ...and the rounding mode is still half-up on the real dialect.
    assert history.json()["games"][0]["summary"]["average_centipawn_loss"] == 3
    assert analysis.json()["summary"]["average_centipawn_loss"] == 3
    assert stats.json()["library"]["avg_blunder_eval_loss_cp"] == 3


# ---------------------------------------------------------------------------
# AST source contract — the default-suite backstop for the cast guard above, which
# skips without PostgreSQL. Deliberately scoped to the CPL call sites: it does not
# police float() elsewhere in these modules, so unrelated future work in
# history.py / session.py / stats.py is not coupled to it.
# ---------------------------------------------------------------------------

_API_DIR = Path(__file__).parent / "app" / "api"
_CPL_SITE_MODULES = ("history.py", "session.py", "stats.py")
_CPL_ENDPOINTS = ("get_history", "get_session_analysis", "get_stats_summary")


def _module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _calls_to(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def _functions(module: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n for n in ast.walk(module) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _references_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


def _is_int_round(node: ast.AST) -> bool:
    """Structural match for ``int(round(...))`` — not a substring, so reformatting
    (or an intervening newline) cannot hide it."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "int"
        and any(
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id == "round"
            for arg in node.args
        )
    )


def _float_in_assignment_chain(func: ast.AST, name: str) -> bool:
    """True if ``name``'s value inside ``func`` was produced by a ``float(...)`` call.

    Follows ``name = <rhs>`` transitively while the right-hand side is itself a bare
    Name (the realistic hoist: ``raw = float(agg)`` … ``x = raw``). Bounded by design —
    a cast laundered through a dict or a helper call slips past. The PostgreSQL spy
    above is what cannot be slipped.
    """
    assignments: dict[str, list[ast.expr]] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assignments.setdefault(node.target.id, []).append(node.value)

    pending, seen = [name], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for rhs in assignments.get(current, []):
            if _calls_to(rhs, "float"):
                return True
            if isinstance(rhs, ast.Name):
                pending.append(rhs.id)
    return False


@pytest.mark.parametrize("module_name", _CPL_SITE_MODULES)
def test_round_half_up_cpl_argument_is_never_an_inline_call(module_name):
    # A1: forbids round_half_up_cpl(float(raw)) — the regression that would silently
    # reintroduce the near-half corruption while every SQLite test still passed.
    module = _module_ast(_API_DIR / module_name)
    calls = _calls_to(module, "round_half_up_cpl")
    assert calls, f"{module_name} has no round_half_up_cpl call"
    for call in calls:
        assert not call.keywords and len(call.args) == 1, (
            f"{module_name}:{call.lineno}: round_half_up_cpl takes one positional arg"
        )
        assert isinstance(call.args[0], (ast.Name, ast.Attribute)), (
            f"{module_name}:{call.lineno}: the database aggregate must be passed "
            f"straight through, not wrapped in a call"
        )


@pytest.mark.parametrize("module_name", _CPL_SITE_MODULES)
def test_round_half_up_cpl_argument_was_never_float_cast(module_name):
    # A2: A1 alone is not enough — hoisting the cast to a local
    # (raw = float(agg) … round_half_up_cpl(raw)) passes A1, because the argument is
    # then a bare Name. Check the argument's own provenance.
    module = _module_ast(_API_DIR / module_name)
    for func in _functions(module):
        for call in _calls_to(func, "round_half_up_cpl"):
            arg = call.args[0] if call.args else None
            if not isinstance(arg, ast.Name):
                continue  # Attribute args are SQLAlchemy row attributes: no local assignment.
            assert not _float_in_assignment_chain(func, arg.id), (
                f"{module_name}:{call.lineno}: {arg.id} was float()-cast before "
                f"reaching round_half_up_cpl; pass the Decimal aggregate through"
            )


def test_every_cpl_aggregating_api_function_rounds_half_up():
    # A3: scoped by PROVENANCE, not by package — every function that touches the
    # read-time normalizer must round through the helper, so a NEW CPL endpoint lands
    # in scope automatically. A blanket ban on int(round(...)) under app/api/ would
    # instead forbid an ordinary idiom in unrelated future endpoints.
    scoped = []
    for path in sorted(_API_DIR.rglob("*.py")):
        for func in _functions(_module_ast(path)):
            if not _references_name(func, "centipawn_loss_expr"):
                continue
            scoped.append(func.name)
            offenders = [n for n in ast.walk(func) if _is_int_round(n)]
            assert not offenders, (
                f"{path.name}:{offenders[0].lineno}: {func.name} aggregates CPL with "
                f"banker's int(round(...)); use round_half_up_cpl"
            )
            assert _calls_to(func, "round_half_up_cpl"), (
                f"{path.name}: {func.name} aggregates CPL but never rounds half-up"
            )

    # stats.py's _round1 (a percentage helper) does not reference centipawn_loss_expr,
    # so its bare round(value, 1) stays out of scope.
    assert "_round1" not in scoped


@pytest.mark.parametrize("endpoint", _CPL_ENDPOINTS)
def test_known_cpl_endpoints_are_in_scope_and_round_half_up(endpoint):
    # A4 (anchor): A3 derives its scope from the centipawn_loss_expr reference, so
    # without this, dropping that reference from a function would silently drop the
    # function out of A3's scope as well. Pin today's three sites by name.
    for path in sorted(_API_DIR.rglob("*.py")):
        for func in _functions(_module_ast(path)):
            if func.name != endpoint:
                continue
            assert _references_name(func, "centipawn_loss_expr")
            assert _calls_to(func, "round_half_up_cpl")
            return
    pytest.fail(f"{endpoint} not found under app/api/")
