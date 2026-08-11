# Drill mode contract

Drill mode is opening practice with a deliberate boundary between guided route play and
ordinary chess evidence. This document records that boundary and the player-visible
lifecycle. The drill routes, models, API payloads, and behavior tests are authoritative
for exact implementation details.

## Start and session lifecycle

A player chooses an opening target from the opening registry or a reachable position in
the openings tree. A drill session starts unrated and carries its own drill outcome in
addition to the normal session lifecycle.

- **active** means the player is working toward the target.
- **root reached** means the server has confirmed the target position.
- **failed** records an off-route failure, a post-root accuracy failure, or a natural game end.
- **abandoned** records an unconverted stop with no earlier failure outcome.
- **converted** marks a drill that became a rated normal game.

The outcome and ordinary session lifecycle are intentionally separate. Stopping a failed
drill ends its session but preserves the failure; only converted drills are visible to
normal game history and statistics.

## Route confirmation and the evidence boundary

The drill route is selected from the opening graph when the target belongs to it, or from
the exact chosen line for an ad-hoc target. The same route selection is used for opponent
steering so guidance and validation cannot choose different routes. In-book routing's
backwards BFS is already transposition-tolerant across recorded graph edges; the routing-only
transposition overlay additionally connects otherwise unrecorded move orders. If that artifact
is unavailable, routing degrades to the base graph rather than failing the drill; ad-hoc targets
always keep their exact-line route.

A route-check normally confirms target arrival. It records both the root-reached state and
a write-once boundary ply when the server can prove the arrival. Serving a suggested move
is not confirmation. The observed-root fallback in opponent-move handling can make the
same transition when its request already proves that the current position is the target.
The client holds play at the root while route-check resolves and retries the same
confirmation rather than advancing play.

Pre-root moves are guided route play. At the current evidence boundary, observations at or
after it seed downstream opportunity discovery, but only observations strictly after it
count as a reached opportunity; when the boundary is the root, the root is therefore a
seed rather than a reach. A drill without a confirmed boundary contributes no broad
evidence. Conversion supplies its own normal-play boundary. Historical repair is
operational maintenance, not a runtime fallback.

## Strictness, terminal outcomes, and conversion

Before the root, leaving the accepted route fails the drill. After the root, strictness
sets the allowed engine-loss threshold; an exact-best setting requires the engine's best
move. A natural game end from either active or root-reached state is also a terminal drill
outcome. The terminal reason records how a failure occurred and is not a substitute for the
session outcome.

A root-reached or failed drill can be converted to rated normal play while its session remains
open. Conversion records the point where normal play begins and resegments moves so rating and
ordinary game policies start there. It does not turn an unconverted practice session into
history.

## Transient drill review

After a stopped drill, Analyze may open an in-memory review of the moves just played.
The snapshot is identity-bound to that session and disappears on refresh or direct entry.
It creates no rating event, saved game review, history row, or normal-game statistic.
Returning to the stopped-drill presentation never revives the ended backend session.

## Repeating a finished drill

When a terminal drill response starts opening-score reconciliation, a direct repeat
action for that same session waits for the provably-fresh result. A fresh response
with no visible score change still releases the action. Poll exhaustion or client
capacity eviction fails open rather than stranding the end screen; a pre-root
off-route failure starts no reconciliation and remains immediately repeatable.
Settings, analysis, ordinary new-game actions, and other departures are not gated.

The initial evaluation uses the full reconciliation lifetime as the repeat gate.
The exact attempt, timeout, failure, accessibility, and telemetry mechanics live in
[opening-score drill-repeat wait telemetry](../opening-delta-drill-wait.md) and the
browser implementation. A fresh result that belongs to a session left through a
non-repeat path retains its previous-session ownership and cannot contaminate the
new session's inline score.

## Authorities

- Lifecycle, conversion, and route validation:
  [backend/app/api/drills.py](../../backend/app/api/drills.py) and
  [backend/app/api/game.py](../../backend/app/api/game.py).
- Route selection and steering:
  [backend/app/drill_steering.py](../../backend/app/drill_steering.py).
- Evidence boundary and opportunity accounting:
  [backend/app/evidence_boundary.py](../../backend/app/evidence_boundary.py).
- Visible-session policy:
  [backend/app/session_contracts.py](../../backend/app/session_contracts.py).
- Browser lifecycle and review behavior:
  [src/hooks/useChessGameLifecycle.ts](../../src/hooks/useChessGameLifecycle.ts),
  [src/components/ChessGame.tsx](../../src/components/ChessGame.tsx), and their tests.
- Repeat-gate timing and event contract:
  [opening-score drill-repeat wait telemetry](../opening-delta-drill-wait.md).
