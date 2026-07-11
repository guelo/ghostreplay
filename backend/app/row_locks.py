from __future__ import annotations

from typing import TypeVar

from sqlalchemy.orm import Query


_QueryT = TypeVar("_QueryT", bound=Query)


def for_no_key_update(query: _QueryT) -> _QueryT:
    """Apply the sanctioned PostgreSQL writer lock shape.

    ``populate_existing`` is required because the row may already be present in
    the Session identity map; the locking read must refresh that instance.
    SQLAlchemy renders ``key_share=True`` (without ``read=True``) as
    ``FOR NO KEY UPDATE`` on PostgreSQL.
    """
    return query.populate_existing().with_for_update(key_share=True)
