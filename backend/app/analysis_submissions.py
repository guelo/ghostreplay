"""Reads over ``analysis_cache_submission`` — submitter eligibility (g-v21l).

An association row means exactly: *this user independently submitted a tuple
consistent with this stored ``analysis_cache`` row*. It is not ownership, confers
no write rights, and is never exposed in an API response, log line, or metric
dimension (see :class:`app.models.AnalysisCacheSubmission`).

Two read shapes exist, deliberately kept apart:

* :func:`viewer_associated_ids` — membership for ONE viewer over an already-loaded
  candidate id set. This is the read path's only association query; the resolved-
  evidence descriptor stamps its immutable ``viewer_associated`` from it, so owner
  scoping is decided from the same snapshot as every other predicate rather than by
  a per-row query during ranking.
* :func:`associated_user_ids_by_row` — the FULL association set per row. Reserved
  for the opening digest's shared projection, which must stay user-independent so
  "scoped digest == full digest's shared slice" holds by construction. The locked
  writer loads full sets too, but through its own ``FOR UPDATE``-bound helper.

The writer's claim pass lives in :mod:`app.analysis_cache_repo`, inside the batch's
own transaction — never here and never after it.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy.orm import Session

from app.models import AnalysisCacheSubmission

# Bind-parameter budget for an expanding ``IN`` list. Mirrors the analysis_cache
# writer's ceiling: PostgreSQL caps the extended protocol at 65535 and SQLite's
# SQLITE_MAX_VARIABLE_NUMBER is 32766, so stay well under the tighter limit and
# split an oversized id set into several ordered statements.
_MAX_IN_PARAMS = 30_000


def _chunks(ids: list[int], size: int = _MAX_IN_PARAMS):
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def viewer_associated_ids(
    db: Session,
    viewer_user_id: int | None,
    cache_ids: Iterable[int],
) -> frozenset[int]:
    """The subset of ``cache_ids`` this viewer holds an association for.

    ``viewer_user_id=None`` means "no viewer" — it admits only effectively
    authoritative rows downstream, so no query is issued. An empty id set likewise
    short-circuits. Issued ONCE per request over the union of every already-loaded
    candidate id, never per capability and never per row.
    """
    if viewer_user_id is None:
        return frozenset()
    ids = sorted({int(i) for i in cache_ids if i is not None})
    if not ids:
        return frozenset()
    found: set[int] = set()
    for chunk in _chunks(ids):
        rows = (
            db.query(AnalysisCacheSubmission.analysis_cache_id)
            .filter(
                AnalysisCacheSubmission.user_id == viewer_user_id,
                AnalysisCacheSubmission.analysis_cache_id.in_(chunk),
            )
            .all()
        )
        found.update(int(r[0]) for r in rows)
    return frozenset(found)


def associated_user_ids_by_row(
    db: Session,
    cache_ids: Iterable[int],
) -> dict[int, tuple[int, ...]]:
    """FULL association sets (sorted user ids) for the given ``analysis_cache`` ids.

    User-INDEPENDENT by construction: the opening evidence digest hashes this so a
    trust flip caused by an association write changes the digest, and it must
    produce the same lines for every user or the scoped-equals-shared-slice
    invariant breaks. Rows with no associations are simply absent from the map.
    """
    ids = sorted({int(i) for i in cache_ids if i is not None})
    if not ids:
        return {}
    out: dict[int, list[int]] = {}
    for chunk in _chunks(ids):
        rows = (
            db.query(
                AnalysisCacheSubmission.analysis_cache_id,
                AnalysisCacheSubmission.user_id,
            )
            .filter(AnalysisCacheSubmission.analysis_cache_id.in_(chunk))
            .all()
        )
        for cache_id, user_id in rows:
            out.setdefault(int(cache_id), []).append(int(user_id))
    return {k: tuple(sorted(v)) for k, v in out.items()}
