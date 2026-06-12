# g-86pd — Free variation pending state on analysis failure (F3)

Split from **g-cache-first-resolve** (was Finding F3 in that plan). Part of epic
**g-stable-drill-best**.

**Depends on g-cache-first-resolve (scope A):** the scoped-variation-error path and the
fatal-teardown / `clearAnalysis` / unmount cleanup paths live in `useMoveAnalysis`, which
scope A rewrites (worker-error request-scoping, mount token). This bead adds the variation
*failure* channel on top of that.

## Problem

On a scoped variation error (or fatal teardown) `useMoveAnalysis` clears
`pendingVariationPlies`, but `useVariationTree.pendingRequestsRef` (`:195-205`) still holds
the entry, so `hasPendingForFen` (AnalysisBoard `:1056`) permanently blocks retrying that
FEN.

## Design — add a reject channel

- Add `rejectPending(requestId)` to `useVariationTree` — deletes from `pendingRequestsRef`
  and bumps `analysisCacheVersion` so `hasPendingForFen` re-evaluates.
- Give `useMoveAnalysis` an optional `onVariationError?(id: string)` callback. Invoke it
  for any `pendingVariationPlies` id on a scoped variation error AND for every still-pending
  variation id during fatal teardown / `clearAnalysis` / unmount. AnalysisBoard wires it to
  `rejectPending` (parallel to how it wires `resolvePending` via `lastAnalysis` at `:971`).
- Variation resolution still flows through `lastAnalysis` → AnalysisBoard `:971`
  `resolvePending`; only the failure path is new.

## Files to modify

- `src/hooks/useVariationTree.ts` — add `rejectPending(id)`.
- `src/hooks/useMoveAnalysis.ts` — add optional `onVariationError?(id)` callback; invoke on
  scoped variation error + fatal teardown/clearAnalysis/unmount.
- `src/components/AnalysisBoard.tsx` — wire `onVariationError` → `rejectPending`.
- Tests: `useVariationTree.test.ts`, `AnalysisBoard.test.tsx`, `useMoveAnalysis.test.ts`.

## Test plan

**17d. Variation error frees retry (Finding F3):** a scoped variation `error` calls
`onVariationError`→`rejectPending`, after which `hasPendingForFen(fen)` is false and the
FEN can be re-requested.

## Acceptance criteria

1. `useVariationTree.rejectPending(requestId)` deletes the pending entry and bumps
   `analysisCacheVersion`.
2. `useMoveAnalysis` invokes `onVariationError(id)` for scoped variation errors and for
   every still-pending variation id during fatal teardown / `clearAnalysis` / unmount.
3. `AnalysisBoard` wires `onVariationError` → `rejectPending`.
4. After a scoped variation error, `hasPendingForFen(fen)` is false and the FEN can be
   re-requested (test 17d).
