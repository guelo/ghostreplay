#!/usr/bin/env python3
"""Regenerate the drill-routing transposition overlay (eco.transpositions.json).

The opening graph is built by replaying ECO move sequences, so a position
recorded under one move order carries no edges for the other orders that reach
it. This script finds every legal move that connects two existing graph
positions without an edge, keeps the ones that make forward progress, and writes
them beside eco.json as a checked-in artifact. Drill routing loads that artifact
in <100ms; the ~37s scan below never runs at app startup.

The edges are routing-only. They are NOT merged into the graph, so
`graph.fingerprint` — which gates the opening score cache and the frozen release
calibration artifact — is unaffected. See app/opening_densify.py.

Usage:
    python scripts/densify_opening_graph.py            # regenerate the artifact
    python scripts/densify_opening_graph.py --check     # CI: verify it is current
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.opening_densify import (  # noqa: E402
    compute_densified_edges,
    graph_topology_fingerprint,
    resolve_artifact_path,
    serialize_edges,
)
from app.opening_graph import build_opening_graph  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("densify")


def _artifact_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    path = resolve_artifact_path()
    if path is None:
        raise SystemExit(
            "Could not resolve the opening data directory. Set OPENING_DATA_DIR."
        )
    return path


def _write(path: Path, payload: dict) -> None:
    """One edge per line: the edges are sorted, so a graph change shows up as a
    readable line-level diff rather than a reflowed blob."""
    header = {k: v for k, v in sorted(payload.items()) if k != "edges"}
    lines = [json.dumps(header, indent=1)[1:-1].rstrip() + ","]
    lines.append(' "edges": [')
    lines.extend(
        f"  {json.dumps(edge)}{',' if i < len(payload['edges']) - 1 else ''}"
        for i, edge in enumerate(payload["edges"])
    )
    lines.append(" ]")
    path.write_text("{" + "\n".join(lines) + "\n}\n")


def _check(path: Path, expected: dict) -> int:
    """Exact-diff the artifact against a fresh recomputation.

    Provenance alone cannot catch this: a truncated or hand-edited artifact keeps
    a valid graph_topology_fingerprint and passes every per-edge validity check,
    because those prove each edge is real — not that every real edge is present.
    Set equality is what proves completeness.
    """
    if not path.is_file():
        logger.error("MISSING: %s does not exist. Run without --check.", path)
        return 1

    try:
        actual = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("UNREADABLE: %s: %s", path, exc)
        return 1

    problems: list[str] = []
    for field in ("schema_version", "graph_topology_fingerprint"):
        if actual.get(field) != expected[field]:
            problems.append(
                f"{field}: artifact has {actual.get(field)!r}, expected {expected[field]!r}"
            )

    expected_edges = {tuple(edge) for edge in expected["edges"]}
    try:
        actual_edges = {tuple(edge) for edge in actual.get("edges", [])}
    except TypeError:
        problems.append("edges: malformed")
        actual_edges = set()

    missing = expected_edges - actual_edges
    extra = actual_edges - expected_edges
    if missing:
        problems.append(f"{len(missing)} edge(s) missing, e.g. {sorted(missing)[0]}")
    if extra:
        problems.append(f"{len(extra)} unexpected edge(s), e.g. {sorted(extra)[0]}")
    if actual.get("edge_count") != len(actual_edges):
        problems.append(
            f"edge_count: declares {actual.get('edge_count')!r}, carries {len(actual_edges)}"
        )

    if problems:
        logger.error("STALE: %s does not match the current graph:", path)
        for problem in problems:
            logger.error("  - %s", problem)
        logger.error("Regenerate: python scripts/densify_opening_graph.py")
        return 1

    logger.info("OK: %s is current (%d edges)", path, len(actual_edges))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the checked-in artifact matches a fresh recomputation exactly; write nothing.",
    )
    parser.add_argument(
        "--out",
        help="Artifact path (defaults to eco.transpositions.json beside eco.json).",
    )
    args = parser.parse_args()

    path = _artifact_path(args.out)

    logger.info("Building opening graph...")
    graph = build_opening_graph()
    logger.info(
        "Graph: %d nodes, %d edges, topology %s",
        graph.node_count,
        graph.edge_count,
        graph_topology_fingerprint(graph)[:12],
    )

    logger.info("Scanning for transposition edges (~37s)...")
    edges = compute_densified_edges(graph)
    logger.info("Retained %d forward-progress transposition edges", len(edges))

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = serialize_edges(graph, edges, generated_at)

    if args.check:
        return _check(path, payload)

    _write(path, payload)
    logger.info("Wrote %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
