"""Count historical sessions exposed by the fresh opening-line proof.

Run before deploying ``raw-v8``:

    python scripts/audit_opening_line_proof.py

This command is read-only and aggregate-only. It never prints session IDs,
users, PGNs, FENs, or moves. SQL/parser failures are reported by exception type
only because detailed database errors may embed row data.
"""

from __future__ import annotations

from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import DATABASE_URL, SessionLocal  # noqa: E402
from app.opening_line_proof_audit import plan_rollout_audit  # noqa: E402


def _safe_database_url(database_url: str) -> str:
    parts = urlsplit(database_url)
    netloc = parts.netloc
    if "@" in netloc:
        credentials, host = netloc.rsplit("@", 1)
        username = credentials.split(":", 1)[0]
        netloc = f"{username}:***@{host}" if username else f"***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def main() -> int:
    print(f"DATABASE_URL={_safe_database_url(DATABASE_URL)}", flush=True)
    db = SessionLocal()
    try:
        audit = plan_rollout_audit(db)
    except Exception as exc:  # noqa: BLE001 - aggregate-only output boundary
        db.rollback()
        print(
            "Opening-line-proof audit aborted: "
            f"{type(exc).__name__} (detail withheld: SQL/parser diagnostics "
            "can embed row data)",
            flush=True,
        )
        return 1
    finally:
        db.rollback()
        db.close()

    print(
        "Opening-line-proof rollout audit: "
        f"evidence_eligible_sessions={audit.evidence_eligible_sessions} "
        f"sessions_with_visible_rows={audit.sessions_with_visible_rows} "
        f"accuracy_failed_sessions={audit.accuracy_failed_sessions} "
        f"accuracy_failed_without_pgn={audit.accuracy_failed_without_pgn} "
        f"bounded_pgn_sessions={audit.bounded_pgn_sessions} "
        f"pgn_unknown_sessions={audit.pgn_unknown_sessions} "
        f"bounded_pgn_without_visible_rows={audit.bounded_pgn_without_visible_rows} "
        f"exact_visible_row_sessions={audit.exact_visible_row_sessions} "
        f"surplus_visible_row_sessions={audit.surplus_visible_row_sessions} "
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
