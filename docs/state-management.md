# Frontend state management

This guide describes the implemented frontend state boundaries. It is an
architecture guide, not a requirement to put all client or server data into a
global store.

## Current model

Ghost Replay uses React state, contexts, and narrow Zustand stores together:

| State or owner | Current authority | Lifetime and boundary |
| --- | --- | --- |
| Gameplay and session state shared across the board workflow | [`useGameStore`](../src/stores/useGameStore.ts) | One non-persisted singleton store shared by the game surface, lifecycle hooks, and analysis connectors. Actions own multi-field transitions that must commit atomically. |
| Live and historical engine-analysis results | [`createAnalysisStore`](../src/stores/createAnalysisStore.ts) | A store factory supports an analysis surface with an explicit provider; the main game uses one singleton instance. Worker lifecycle and per-move analysis state stay separate from general game state. |
| Drill-analysis route handoff | [`drillAnalysisStore`](../src/stores/drillAnalysisStore.ts) | A deliberately ephemeral singleton. Refreshing drops the snapshot because there is no durable drill-analysis endpoint. |
| Authentication and account lifecycle | [`AuthContext`](../src/contexts/AuthContext.tsx) | Context owns account initialization, credentials, bearer-token lifecycle, and the auth operations consumed throughout the app. |
| Analysis orchestration service | [`GameAnalysisCoordinatorContext`](../src/contexts/GameAnalysisCoordinatorContext.tsx) | Context exposes the long-lived coordinator instance; the coordinator and its effects are not store state. |
| Component- or page-local state | Components and focused hooks | Loading and error states, form drafts, UI toggles, and request results remain local when no cross-boundary consumer needs them. [`useOpeningsTree`](../src/hooks/useOpeningsTree.ts) is an example with a workflow-specific response cache and stale-request guard. |

Mutable `Chess` instances stay outside Zustand and are passed to controller
hooks such as [`useChessGameController`](../src/hooks/useChessGameController.ts).
Effects and request sequencing belong in hooks or services rather than raw
store subscribers. Store state records the inputs and outcomes that rendering
or another workflow boundary needs.

## Server data

The frontend does not use TanStack Query or another general server-state cache.
Typed functions in [`src/utils/api.ts`](../src/utils/api.ts) call `fetch` through
the shared `requestJson` transport, which owns error decoding, bounded retries,
request correlation, deadlines, and telemetry. Authentication uses a focused
fetch wrapper because its initialization flow has different retry and error
semantics.

The calling page, hook, or lifecycle owns when a request runs and how its result
is refreshed, cancelled, cached, or discarded as stale. A response belongs in
Zustand only when a concrete cross-component workflow needs the same client-side
state or an atomic transition. PostgreSQL and the FastAPI contract remain the
durable source of truth; copying a response into a store does not make it more
authoritative.

## Store design rules

When adding or changing frontend state:

1. Keep state local unless it crosses a real component, route, or lifecycle
   boundary.
2. Put only canonical values in a store. Derive cheap display values in
   selectors or subscriber components so reset, rewind, navigation, and move
   application cannot make stored copies drift.
3. Keep mutable engines, timers, network work, browser effects, and coordinator
   services outside stores. Hooks and services perform effects and commit their
   outcomes through store actions.
4. Give every stored value an explicit lifetime and reset path. Non-persisted
   singletons survive component remounts but not a page reload; route handoffs
   that must survive reload need a backend contract rather than accidental
   client persistence.
5. Prefer narrow selectors and purpose-specific stores over a single application
   store. Add a new slice only when its values share ownership and reset
   semantics with that slice.
6. Test behavior contracts and transitions. The focused store tests and the
   lifecycle/component tests that consume them are the authority for exact
   reset and sequencing behavior.

Adding a generalized server-data library would be an architecture change. It
should start from a demonstrated cache or synchronization need, define which
existing request lifecycles it replaces, and update this guide together with
the provider and dependency changes.
