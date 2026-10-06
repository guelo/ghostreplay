import pytest

from scripts.bench_opening_score_cutover import assert_server, guarded_url, PRODUCTION_ENVIRONMENT
from scripts import bench_opening_score_cutover as runner


def inputs():
    manifest = {"environment_id": "isolated", "runner_service_id": "runner",
                "database_host": "cutover.railway.internal"}
    env = {"RAILWAY_ENVIRONMENT_ID": "isolated", "RAILWAY_SERVICE_ID": "runner",
           "RAILWAY_ENVIRONMENT_NAME": "score-store-cutover",
           "CUTOVER_DATABASE_URL": "postgresql://fixture:fake@cutover.railway.internal:5432/railway"}
    return manifest, env


def test_guard_selects_only_named_disposable_database():
    manifest, env = inputs()
    url = guarded_url(manifest, "gr_score_qual_cutover_s1_c1", env)
    assert url.database == "gr_score_qual_cutover_s1_c1"
    assert url.drivername == "postgresql+psycopg"


@pytest.mark.parametrize("key,value", [
    ("RAILWAY_ENVIRONMENT_ID", PRODUCTION_ENVIRONMENT),
    ("RAILWAY_SERVICE_ID", "different"),
    ("RAILWAY_ENVIRONMENT_NAME", "production"),
    ("DATABASE_URL", "postgresql://localhost/dev"),
    ("PGHOST", "localhost"),
    ("CUTOVER_DATABASE_URL", "postgresql://fixture:fake@production.railway.internal/railway"),
    ("CUTOVER_DATABASE_URL", "postgresql://fixture:fake@cutover.railway.internal/railway?host=production"),
])
def test_guard_refuses_wrong_target_or_ambient_configuration(key, value):
    manifest, env = inputs()
    env[key] = value
    with pytest.raises(ValueError):
        guarded_url(manifest, "gr_score_qual_cutover_s1_c1", env)


@pytest.mark.parametrize("database", ["railway", "postgres", "gr_snap_base", "gr_score_qual_other"])
def test_guard_refuses_non_measurement_database(database):
    manifest, env = inputs()
    with pytest.raises(ValueError):
        guarded_url(manifest, database, env)


def test_manifest_cannot_opt_into_production():
    manifest, env = inputs()
    manifest["environment_id"] = env["RAILWAY_ENVIRONMENT_ID"] = PRODUCTION_ENVIRONMENT
    with pytest.raises(ValueError):
        guarded_url(manifest, "gr_score_qual_cutover_s1_c1", env)


@pytest.mark.parametrize("mismatch", [None, 0, 1, 2, 3])
def test_server_seal_requires_database_cluster_system_id_and_comment(mismatch):
    database = "gr_score_qual_cutover_s1_c1"
    manifest = {"cluster_name": "isolated", "system_identifier": "123", "sentinel": "run-seal"}
    row = [database, "isolated", "123", "run-seal"]
    if mismatch is not None:
        row[mismatch] = "wrong"

    class Connection:
        def execute(self, statement):
            assert str(statement).startswith("SELECT")
            return self

        def one(self):
            return row

    if mismatch is None:
        assert_server(Connection(), manifest, database)
    else:
        with pytest.raises(ValueError, match="seal mismatch"):
            assert_server(Connection(), manifest, database)


def test_runtime_seal_refuses_a_new_dependency_version(monkeypatch):
    monkeypatch.setattr(runner.platform, "python_version", lambda: "3.12.7")
    monkeypatch.setattr(runner.importlib.metadata, "version", lambda name: "2.0.54")
    manifest = {"runtime": {"python": "3.12.7", "packages": {"SQLAlchemy": "2.0.54"}}}
    assert runner.assert_runtime(manifest) == manifest["runtime"]
    monkeypatch.setattr(runner.importlib.metadata, "version", lambda name: "2.1.0")
    with pytest.raises(ValueError, match="dependencies differ"):
        runner.assert_runtime(manifest)
