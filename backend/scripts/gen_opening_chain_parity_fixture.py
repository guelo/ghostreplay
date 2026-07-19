"""Generate the shared opening-chain parity fixture (g-a5v3).

The live opening cards are derived CLIENT-side (src/openings/deriveLiveLineage.ts)
so they render on the same tick as the move, while the backend derives the same
chain server-side (app/opening_roots.py::played_opening_chain_indexed). Two
implementations of one walk can drift; this fixture is the shared ground truth
that pins them together.

Run from backend/ with the venv active:

    python scripts/gen_opening_chain_parity_fixture.py

Writes src/openings/__fixtures__/openingChainParity.json (consumed by BOTH
backend/test_opening_chain_parity.py and
src/openings/deriveLiveLineage.parity.test.ts).
"""

from __future__ import annotations

import json
from pathlib import Path

import chess

from app.opening_graph import _fen_from_board

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "src" / "openings" / "__fixtures__" / "openingChainParity.json"


def key_after(sans: list[str]) -> str:
    """The normalized 4-field opening key of the position after `sans`."""
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    return _fen_from_board(board)


def fens_after(sans: list[str]) -> list[str]:
    """Full FEN after each move (what a client move record stores)."""
    board = chess.Board()
    out = []
    for san in sans:
        board.push_san(san)
        out.append(board.fen())
    return out


# ---------------------------------------------------------------------------
# Root registry — a small synthetic set, so the fixture does not depend on the
# real (~30s to build) opening graph. Only the walk is under test, not the
# registry's contents.
# ---------------------------------------------------------------------------

ROOT_SPECS = [
    (["e4"], "King's Pawn", "King's Pawn", "B00", 1),
    (["e4", "e5", "Nf3", "Nc6"], "Three Knights Setup", "King's Pawn", "C44", 4),
    (["e4", "e5", "Nf3", "Nc6", "Bb5"], "Ruy Lopez", "Ruy Lopez", "C60", 5),
    # Transposition target: reachable as 1.d4 Nf6 2.c4 AND 1.c4 Nf6 2.d4.
    (["d4", "Nf6", "c4"], "Indian Defense", "Indian Defense", "A45", 3),
    # En-passant sensitive: after 1.e4 Nf6 2.e5 d5 the ep square d6 IS legally
    # capturable, so it survives normalization and is PART of this root's key.
    (["e4", "Nf6", "e5", "d5"], "Alekhine Exchange-ish", "Alekhine Defense", "B03", 4),
    # The other half of the invariant: a root whose key has NO ep square even
    # though the position was reached by a double push. See RAW_EP_CASE — the
    # key is registered with "-" and a raw FEN carrying the uncapturable square
    # must still normalize onto it.
    (["a3", "e5", "h3", "d5"], "Uncapturable EP", "Irregular", None, 4),
    (["h4", "a5", "Rh3"], "Nonsense Opening", "Irregular", None, 3),
    # A pair of knight-move roots that a retreat sequence re-reaches, so the
    # chain goes A -> B -> A. Needed to exercise the RETENTION rule: the dedupe
    # compares against the chain TAIL, so a re-crossing is only retained when a
    # different root was crossed in between.
    (["Nf3"], "Zukertort Opening", "Reti", "A04", 1),
    (["Nf3", "Nf6"], "Zukertort Symmetrical", "Reti", "A05", 2),
]


def build_roots() -> list[dict]:
    return [
        {
            "opening_key": key_after(sans),
            "opening_name": name,
            "opening_family": family,
            "eco": eco,
            "depth": depth,
        }
        for sans, name, family, eco, depth in ROOT_SPECS
    ]


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

CASES = [
    {
        "name": "linear walk through nested roots",
        "sans": ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"],
    },
    {
        "name": "transposition: d4/c4 move order reaches the Indian root",
        "sans": ["d4", "Nf6", "c4", "e6"],
    },
    {
        "name": "transposition: c4/d4 move order reaches the SAME root",
        "sans": ["c4", "Nf6", "d4", "e6"],
    },
    {
        # Chain is A(0) -> B(1) -> A(4): the Zukertort root is crossed, left via
        # the knight retreats, and re-crossed. Because a DIFFERENT root sits
        # between them, the re-crossing is retained with its OWN index — and so
        # its own, longer SAN prefix.
        "name": "non-consecutive repeated root retains both crossings",
        "sans": ["Nf3", "Nf6", "Ng1", "Ng8", "Nf3"],
    },
    {
        # The mirror image: the same root re-reached with NO other root in
        # between collapses onto the first crossing (dedupe is against the
        # chain tail, not the move index).
        "name": "repeat with no root in between is deduped",
        "sans": ["e4", "e5", "Nf3", "Nc6", "Ng1", "Nb8", "Nf3", "Nc6"],
    },
    {
        "name": "no roots crossed",
        "sans": ["a3", "h6", "a4", "h5"],
    },
    {
        "name": "en passant legally available at the root position",
        "sans": ["e4", "Nf6", "e5", "d5"],
    },
    {
        # See RAW_EP_CASE for the injected variant that actually exercises the
        # has_legal_en_passant() gate.
        "name": "double push whose ep square is not legally capturable",
        "sans": ["a3", "e5", "h3", "d5"],
    },
    {
        "name": "empty game",
        "sans": [],
    },
]


# A hand-injected case: the FEN after 1.a3 e5 2.h3 d5 but with the en-passant
# square d6 written back in EXPLICITLY.
#
# This cannot be produced by replaying moves. BOTH python-chess's board.fen()
# and chess.js's fen() already canonicalize a non-capturable ep square to "-",
# so a fixture derived from move replay can never carry one — which is exactly
# why the has_legal_en_passant() gate went untested until now.
#
# Injecting the raw FEN forces each side's normalizer to do the stripping
# itself. If either stopped gating on legality, it would emit a key ending
# "KQkq d6" while the other emitted "KQkq -", the root lookup would miss, and
# the live and history cards would silently disagree.
RAW_EP_CASE = {
    "name": "raw FEN with a non-capturable ep square normalizes onto the root",
    "sans": ["a3", "e5", "h3", "d5"],
    "raw_fens": [
        None,
        None,
        None,
        "rnbqkbnr/ppp2ppp/8/3pp3/8/P6P/1PPPPPP1/RNBQKBNR w KQkq d6 0 3",
    ],
}


def main() -> None:
    roots = build_roots()
    roots_by_key = {r["opening_key"]: r for r in roots}

    cases = []
    for case in [*CASES, RAW_EP_CASE]:
        sans = case["sans"]
        fens = fens_after(sans)
        # Substitute any hand-authored raw FEN over the replayed one.
        for i, raw in enumerate(case.get("raw_fens") or []):
            if raw is not None:
                fens[i] = raw
        # Expected chain, computed by walking the same rule the two
        # implementations must independently reproduce.
        expected = []
        for index, fen in enumerate(fens):
            key = _fen_from_board(chess.Board(fen))
            root = roots_by_key.get(key)
            if root is None:
                continue
            if expected and expected[-1]["opening_key"] == root["opening_key"]:
                continue
            expected.append(
                {
                    "opening_key": root["opening_key"],
                    "crossing_index": index,
                    "moves": sans[: index + 1],
                }
            )
        # path is the keys of all PRIOR entries.
        for position, entry in enumerate(expected):
            entry["path"] = [e["opening_key"] for e in expected[:position]]

        cases.append({"name": case["name"], "sans": sans, "fens": fens, "expected": expected})

    payload = {
        "_comment": (
            "GENERATED by backend/scripts/gen_opening_chain_parity_fixture.py — "
            "do not hand-edit. Shared ground truth pinning "
            "played_opening_chain_indexed (backend) to deriveLiveOpeningLineage "
            "(frontend). Regenerate after changing either walk."
        ),
        "roots": roots,
        "cases": cases,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {OUT_PATH} ({len(cases)} cases, {len(roots)} roots)")


if __name__ == "__main__":
    main()
