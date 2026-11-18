import { defineRailway, github, project, service } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "ghostreplay";
export default defineRailway(() => {
  const ghostreplay = service("ghostreplay", {
    source: github("guelo/ghostreplay", { branch: "master", checkSuites: false, rootDirectory: "/backend" }),
    build: {
      buildCommand: "if [ -f scripts/prepare_maia3_engine.py ]; then /opt/venv/bin/python scripts/prepare_maia3_engine.py; fi",
      buildEnvironment: "V3",
      builder: "NIXPACKS",
    },
    start: "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT",
    healthcheck: "/health",
    healthcheckTimeout: 30,
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 3 },
    replicas: { "sfo": 1 },
  });

  return project("ghostreplay", {
    // Required: without it the plan refuses, because Railway holds variables this
    // file does not declare. The SDK's types omit the field, so tsc rejects this
    // line, but the CLI honours it.
    variables: { managed: false },
    resources: [ghostreplay],
  });
});
