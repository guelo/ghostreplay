import pytest

from app.database_url import LOCAL_DATABASE_URL, resolve_database_url


DATABASE_ENV_NAMES = (
    "DATABASE_URL",
    "DATABASE_PRIVATE_URL",
    "DATABASE_PUBLIC_URL",
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RENDER",
    "RENDER_SERVICE_ID",
)


@pytest.fixture(autouse=True)
def clear_database_env(monkeypatch):
    for env_name in DATABASE_ENV_NAMES:
        monkeypatch.delenv(env_name, raising=False)


def test_resolve_database_url_defaults_to_local_database():
    assert resolve_database_url() == LOCAL_DATABASE_URL


def test_resolve_database_url_normalizes_database_url_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@postgres.railway.internal:5432/db")

    assert (
        resolve_database_url()
        == "postgresql+psycopg://user:pass@postgres.railway.internal:5432/db"
    )


def test_resolve_database_url_normalizes_postgresql_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@postgres.railway.internal:5432/db")

    assert (
        resolve_database_url()
        == "postgresql+psycopg://user:pass@postgres.railway.internal:5432/db"
    )


def test_resolve_database_url_preserves_encoded_database_url_credentials(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgres://user:p%2Fa%3Ass%40word@postgres.railway.internal:5432/db",
    )

    assert (
        resolve_database_url()
        == "postgresql+psycopg://user:p%2Fa%3Ass%40word@postgres.railway.internal:5432/db"
    )


def test_resolve_database_url_uses_railway_pg_variables(monkeypatch):
    monkeypatch.setenv("PGHOST", "postgres.railway.internal")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGDATABASE", "railway")
    monkeypatch.setenv("PGUSER", "postgres")
    monkeypatch.setenv("PGPASSWORD", "p/a:ss@word")

    assert (
        resolve_database_url()
        == "postgresql+psycopg://postgres:p%2Fa%3Ass%40word@"
        "postgres.railway.internal:5432/railway"
    )


def test_resolve_database_url_uses_postgres_alias_variables(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "postgres.railway.internal")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "railway")
    monkeypatch.setenv("POSTGRES_USER", "postgres")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    assert (
        resolve_database_url()
        == "postgresql+psycopg://postgres:secret@postgres.railway.internal:5433/railway"
    )


def test_resolve_database_url_brackets_ipv6_pg_host(monkeypatch):
    monkeypatch.setenv("PGHOST", "::1")
    monkeypatch.setenv("PGDATABASE", "railway")
    monkeypatch.setenv("PGUSER", "postgres")
    monkeypatch.setenv("PGPASSWORD", "secret")

    assert resolve_database_url() == "postgresql+psycopg://postgres:secret@[::1]:5432/railway"


def test_resolve_database_url_fails_in_railway_without_database(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

    with pytest.raises(RuntimeError, match="Database configuration is missing"):
        resolve_database_url()


def test_resolve_database_url_fails_in_render_without_database(monkeypatch):
    monkeypatch.setenv("RENDER", "true")

    with pytest.raises(RuntimeError, match="Database configuration is missing"):
        resolve_database_url()
