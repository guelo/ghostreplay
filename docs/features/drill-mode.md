# Drill mode contract

Drill mode is opening practice with a deliberate boundary between guided route play and
ordinary chess evidence. This document records that boundary and the player-visible
lifecycle. The drill routes, models, API payloads, and behavior tests are authoritative
for exact implementation details.

## Start and session lifecycle

A player chooses an opening target from the opening registry, a reachable position in
the openings tree, or a played opening card on /play or /history. A drill session starts
unrated and carries its own drill outcome in addition to the normal session lifecycle.

- **active** means the player is working toward the target.
- **root reached** means the server has confirmed the target position.
- **failed** records an off-route failure, a post-root accuracy failure, or a natural game end.
- **abandoned** records an unconverted stop with no earlier failure outcome.
- **converted** is a legacy state for drills that previously became rated normal games.
  New drills cannot convert to normal play.

The outcome and ordinary session lifecycle are intentionally separate. Stopping a failed
drill ends its session but preserves the failure; only legacy converted drills are visible to
normal game history and statistics.

## Route confirmation and the evidence boundary

In automatic mode, the drill route is selected from the opening graph when the target
belongs to it, or from the exact chosen line for an ad-hoc target. The same route selection is used for opponent
steering so guidance and validation cannot choose different routes. In-book routing's
backwards BFS is already transposition-tolerant across recorded graph edges; the routing-only
transposition overlay additionally connects otherwise unrecorded move orders. If that artifact
is unavailable, routing degrades to the base graph rather than failing the drill; ad-hoc targets
keep their exact-line route in automatic mode.

Played opening cards use **preferred route guidance** (`prefer_line`). The selected
occurrence's actual prefix, replayed from the standard start, supplies the opponent's
preferred continuation at each normalized position. Other routes remain valid when
they can reach the target through the known graph, routing overlay, or the supplied
line's legal edges. After an accepted deviation the opponent uses target-directed
guidance; when play transposes back onto the saved route it resumes the preference,
regardless of the history or ply used to get there. Unknown routes still fail off-route;
this does not perform arbitrary forward chess search.

An unavailable, illegal, overlong, nonstandard-start, repeated-position, or mismatched
card prefix disables only its drill action, with an accessible explanation. Setup
shows the guidance mode. Confirming a List or Tree picker selection selects automatic
guidance, including when the target is unchanged; tentative exploration and cancel
preserve the preference. Again, settings, and analysis return retain the accepted mode,
line, and full server opening metadata. Registry depth is independent of line length.

Preference affects opponent selection only before the target. Player and opponent
root confirmation retain their existing evidence proofs and record the actual arrival
ply, which can differ from the saved line length. Post-root and legacy converted play retain
the behavior below. Saved-route replay and combined reverse BFS run before acquiring
the opponent endpoint's session row lock; refreshed state and cached decisions keep
their existing precedence. Supplemental route edges never modify shared graph or
target-route caches.

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
evidence. Legacy converted sessions retain their recorded normal-play boundary. Historical repair is
operational maintenance, not a runtime fallback.

For a registered target, opponent guidance continues after the root while the live position
has a score-relevant opening continuation. Base reference moves take priority and follow the
quality scorer's child-only opening boundary. When no eligible base reference move remains,
the routing-only transposition overlay may supply a continuation using Coverage's stricter
parent-and-child opening boundary. This keeps practice evidence inside a subtree that can
affect the selected opening's score without changing scorer weights or evidence rules.

A due Ghost target whose first move parses to an allowed continuation keeps ordinary Ghost
priority and metadata. If none is due, the server chooses a stable structural move for that
session and position. Off-graph positions, exhausted opening topology, ad-hoc exact-line
targets after their root, and legacy converted drills use the ordinary unconstrained Ghost-then-Maia
pipeline. Structural guidance uses the existing Ghost response mode, so the board displays
**Replay Ghost** during that phase and returns to Maia presentation at the boundary. A later
transposition back into structural topology can therefore produce the existing “The haunting
resumes” presentation.

## Strictness and terminal outcomes

Before the root, leaving the accepted route fails the drill. After the root, strictness
sets the allowed engine-loss threshold; an exact-best setting requires the engine's best
move. A natural game end from either active or root-reached state is also a terminal drill
outcome. The terminal reason records how a failure occurred and is not a substitute for the
session outcome.

Stopped drills remain unrated and outside normal game history. The former
"Continue as normal game" action and its API endpoint have been removed. Historical
converted sessions retain their stored rating boundary, normal-game visibility, and
game-end behavior; removing the action does not migrate those rows.

## Transient drill review

After a stopped drill, Analyze may open an in-memory review of the moves just played.
Analyze finalizes the stopped drill through abandon, preserving any failure outcome.
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

The accepted repeat gate uses the full reconciliation lifetime.
The exact attempt, timeout, failure, accessibility, and telemetry mechanics live in
[opening-score drill-repeat wait telemetry](../opening-delta-drill-wait.md) and the
browser implementation. Score changes appear only for the current session, in
the opening cards and post-game banner. Results arriving after a successful
replacement are suppressed; there is no previous-drill notification. Old terminal
polls still finish or are evicted and report their actual completion outcome.

While a replacement request is pending, the old session keeps ownership of its
score. Freshness may arrive during that request, but repeat controls stay disabled
until the start settles. A failed start retains that fresh score and releases the
repeat gate; an already-abandoned drill stays ended. Visiting drill analysis and
returning without replacing the session preserves its reconciliation and gate.

## Rollout

Roll out the additive `20260919_02` migration and backend before the frontend producer.
Existing rows and omitted request modes remain `auto`. A preferred start requires the
server to echo `prefer_line`; an older or incompatible response produces a start error
and best-effort cleanup of the returned session, without making it playable. No graph
rebuild, artifact regeneration, score recomputation, or production repair is required.
The model edit changes scorer source provenance, not the scoring formula.

## Authorities

- Lifecycle and route validation:
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
- Played-card route validation and complete setup selections:
  [src/openings/lineageDrill.ts](../../src/openings/lineageDrill.ts) and
  [src/openings/drillSelection.ts](../../src/openings/drillSelection.ts).
- Repeat-gate timing and event contract:
  [opening-score drill-repeat wait telemetry](../opening-delta-drill-wait.md).
