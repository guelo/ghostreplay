# Production release checklist

Use this checklist for a release that changes both the Railway API and the
Vercel client. Feature-specific runbooks may add stricter gates.

## Before promotion

1. Record the release commit and the backend/frontend changes in the owning
   Beads issue.
2. Run the repository quality gates and keep their results in that issue.
3. Identify cross-tier protocol changes and choose an order in which every
   mixed-version state is safe.
4. Do not promote both tiers concurrently when one tier requires a capability
   introduced by the other. Hold the dependent tier until its prerequisite is
   active and verified.

## Late evaluation repair (`g-residual-eval-gaps`)

The backend must become active before the frontend is promoted:

1. Deploy the backend artifact containing
   `POST /api/session/{session_id}/moves/eval-repair`.
2. Wait for the Railway deployment to become healthy and for the previous
   deployment to drain.
3. Verify the active backend's `/openapi.json` contains the exact
   `/api/session/{session_id}/moves/eval-repair` path. A generic `/moves` route
   is not sufficient.
4. Only then promote the frontend artifact that emits `late_eval_repair`.
5. Smoke-test an ended game and verify ordinary terminal behavior remains
   bounded. Record the backend deployment id, frontend deployment id, route
   verification, and smoke-test result in `g-residual-eval-gaps`.

The dedicated route is a fail-closed compatibility guard: if the frontend
reaches an old backend, that backend returns 404 instead of silently accepting
the sparse payload as an ordinary move upload. The frontend abandons the repair
after six total attempts. This prevents stale-full-overwrites-repair corruption,
but it does not make frontend-first acceptable—the late evaluation can still be
lost after the bounded retries.

## After promotion

1. Confirm both production health endpoints and the browser's API rewrite reach
   the intended backend deployment.
2. Run the feature-specific production verification.
3. Record deployment identifiers, timestamps, verification results, and any
   rollback decision in the owning Beads issue.
