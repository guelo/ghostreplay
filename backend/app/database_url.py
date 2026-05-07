from __future__ import annotations

import os
from urllib.parse import quote

LOCAL_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/ghostreplay"


def _normalize_postgres_scheme(database_url: str) -> str:
    # DATABASE_URL is expected to be a complete, already URL-encoded SQLAlchemy URL.
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


def _format_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _database_url_from_pg_env() -> str | None:
    host = os.getenv("PGHOST") or os.getenv("POSTGRES_HOST")
    database = os.getenv("PGDATABASE") or os.getenv("POSTGRES_DB")
    user = os.getenv("PGUSER") or os.getenv("POSTGRES_USER")
    password = os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD")
    port = os.getenv("PGPORT") or os.getenv("POSTGRES_PORT") or "5432"

    if not all([host, database, user, password]):
        return None

    return (
        "postgresql+psycopg://"
        f"{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{_format_host(host)}:{port}/{quote(database, safe='')}"
    )


def resolve_database_url() -> str:
    for env_name in ("DATABASE_URL", "DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL"):
        database_url = os.getenv(env_name)
        if database_url:
            return _normalize_postgres_scheme(database_url)

    pg_database_url = _database_url_from_pg_env()
    if pg_database_url:
        return pg_database_url

    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
        raise RuntimeError(
            "Database configuration is missing. Set DATABASE_URL or attach Railway "
            "Postgres variables to this service."
        )

    return LOCAL_DATABASE_URL
