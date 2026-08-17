"""Repair verified historical rows before deploying the fresh line proof.

Dry-run classification is the default:

    python scripts/repair_opening_line_proof_rows.py

Applying requires explicit authorization:

    python scripts/repair_opening_line_proof_rows.py --apply

Output is aggregate-only. The command never prints session IDs, users, PGNs,
FENs, moves, or evaluation values; failures expose only the exception type.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import DATABASE_URL, SessionLocal  # noqa: E402
from app.opening_line_proof_backfill import run_backfill  # noqa: E402


def _safe_database_url(database_url: str) -> str:
    parts = urlsplit(database_url)
    netloc = parts.netloc
    if "@" in netloc:
        credentials, host = netloc.rsplit("@", 1)
        username = credentials.split(":", 1)[0]
        netloc = f"{username}:***@{host}" if username else f"***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit verified repairs. Omit for the read-only dry-run plan.",
    )
    return parser.parse_args()


def _safe_sqlstate(exc: BaseException) -> str:
    code = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if isinstance(code, str) and len(code) == 5 and code.isalnum():
        return code
    return "unknown"


def main() -> int:
    args = parse_args()
    print(f"DATABASE_URL={_safe_database_url(DATABASE_URL)}", flush=True)
    try:
        run = run_backfill(SessionLocal, apply=args.apply)
    except Exception as exc:  # noqa: BLE001 - aggregate-only output boundary
        print(
            "Opening-line-proof repair aborted: "
            f"{type(exc).__name__} sqlstate={_safe_sqlstate(exc)} "
            "(detail withheld: SQL/parser diagnostics can embed row data)",
            flush=True,
        )
        return 1

    audit = run.plan.audit
    outcome = run.outcome
    print(
        "Opening-line-proof repair plan: "
        f"proof_short_sessions={audit.proof_short_sessions} "
        f"proof_short_missing_rows={audit.proof_short_missing_rows} "
        f"physical_row_short_sessions={audit.physical_row_short_sessions} "
        f"physical_missing_rows={audit.physical_missing_rows} "
        f"null_fen_short_sessions={audit.null_fen_short_sessions} "
        f"null_fen_filtered_rows={audit.null_fen_filtered_rows} "
        f"repairable_physical_short_sessions="
        f"{audit.repairable_physical_short_sessions} "
        f"unrepairable_physical_short_sessions="
        f"{audit.unrepairable_physical_short_sessions}",
        flush=True,
    )
    print(
        f"Opening-line-proof repair outcome ({'applied' if run.applied else 'dry-run'}): "
        f"sessions_attempted={outcome.sessions_attempted} "
        f"sessions_repaired={outcome.sessions_repaired} "
        f"rows_inserted={outcome.rows_inserted} "
        f"evidence_sessions_bumped={outcome.evidence_sessions_bumped} "
        f"sessions_no_longer_repairable={outcome.sessions_no_longer_repairable}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
