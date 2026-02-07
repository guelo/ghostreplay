# Maia-2 Runtime Feasibility (ghostreplay-29c.1)

Date: 2026-02-06
Issue: `ghostreplay-29c.1`
Type: research spike

## Decision

- Browser Maia-2 runtime (now): `NO-GO`
- Backend Maia-2 engine service (now): `GO`
- Architecture choice: `backend` with a single opponent-move endpoint

## Why

- Current Maia-2 integration path is Python + PyTorch checkpoint loading (`torch.load`) with Google Drive model downloads.
- There is no existing browser/JS/WASM Maia-2 runtime path in this repo.
- Model artifact footprint is large (`maia2/maia2_models/rapid_model.pt` is ~267 MB), making browser delivery and startup risky for MVP.
- The current frontend opponent logic is split (`/api/game/ghost-move` + local engine fallback), but both decisions now belong in backend orchestration.

## Current State Confirmed In Repo

- Frontend currently performs split decisioning:
  - Ghost check via API in `src/hooks/useOpponentMove.ts`
  - Local engine fallback in `src/components/ChessGame.tsx`
- Backend currently exposes ghost-only move lookup in `backend/app/api/game.py`
- Analysis remains browser-side Stockfish worker (`src/workers/analysisWorker.ts`) for blunder/SRS evaluation.

## Recommended Runtime Architecture

- Keep browser-side analysis worker for blunder detection unchanged.
- Move opponent move selection fully to backend:
  - First attempt ghost steering (graph traversal).
  - If no ghost path, run backend engine inference (Maia-2).
  - Return one move response to client.

This removes split orchestration from frontend and creates one authoritative opponent decision path.

## Proposed Unified Endpoint

Endpoint:
- `POST /api/game/next-opponent-move`

Request:

```json
{
  "session_id": "uuid",
  "fen": "string"
}
```

Response:

```json
{
  "mode": "ghost | engine",
  "move": {
    "uci": "string",
    "san": "string"
  },
  "target_blunder_id": "integer | null",
  "decision_source": "ghost_path | backend_engine"
}
```

Notes:
- `mode: "ghost"`: move comes from graph path to a due blunder.
- `mode: "engine"`: move comes from backend engine inference (Maia-2 path).
- `move` is non-null when legal moves exist; null only on terminal positions.
- Client should no longer run local opponent engine in normal operation.

## Backend Execution Model

- Synchronous request/response for MVP.
- Process-level model cache for loaded Maia model and prepared inference artifacts.
- Optional warmup hook later if cold starts are problematic.

## Key Risks

- Backend dependency/runtime setup (Torch + Maia compatibility) must be pinned and validated.
- Cold-start latency and memory footprint per worker.
- Throughput under concurrent games must be measured in target infra.

## Phased Plan

1. Runtime bring-up
- Add/pin Maia runtime deps in backend and verify local inference call.

2. Unified endpoint MVP
- Implement `/api/game/next-opponent-move` with ghost-first, engine-second orchestration.
- Add validation and error envelope support.

3. Frontend integration
- Replace split ghost/local-engine flow with one backend call.
- Keep graceful fallback behavior for API/network failure.

4. Perf hardening
- Benchmark p50/p95 latency and memory.
- Add timeouts, logging, and basic load guardrails.

## Outcome For Parent (`ghostreplay-29c`)

- Runtime placement decision is unblocked: backend opponent engine service.
- Next work should focus on implementing the unified endpoint and wiring it into frontend move application.
