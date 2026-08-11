# Stats and metrics contract

The stats summary answers several different player questions, so its metrics intentionally
do not share one denominator. This document records the population policy; the stats API
and its tests are authoritative for query and calculation details.

## Scope

The Games, Moves, and Colors cards are scoped to the signed-in player's visible sessions
in the requested time window. Visible sessions are normal games and drills converted to
normal play, so failed and unconverted drills do not enter those populations.

The remaining card families intentionally use other scopes: Training combines all-time
retention with review-date window measures, Library combines all-time and creation-date
measures, and Openings reads the latest cached score batch. The summary therefore does not
apply one session/window predicate to every statistic.

## Move and game populations

| Metric | Grain | Population |
|---|---|---|
| Quality distribution | Move | Classified player moves in every visible windowed session, including active games |
| Mistake-free game rate | Game | All ended visible sessions in the window |
| Accuracy percentage | Game | Ended visible sessions that have a cached accuracy value |

Including active games in the move distribution is intentional: their classified moves are
already evidence of current play. Game-level rates wait for a game to finish because an
active game has not yet had the chance to contain a mistake.

Accuracy is a per-game player measure, not a move-weighted aggregate. The service averages
the cached, already rounded game accuracies and rounds the resulting mean. A game whose
accuracy could not be safely computed has no cached value and is excluded from this one
mean; it remains in the mistake-free population. Treating an unscored game as zero would
turn missing evidence into a false performance claim.

The three Moves metrics, and the color-scoped accuracy percentages, are **null** rather
than zero when their relevant population is empty. Clients must test explicitly for null so
an honest zero is still displayable. Other summary metrics choose their own empty-state
semantics; for example, an empty library can report zero average blunder loss.

## Authorities

- Population, calculation, and response contract:
  [backend/app/api/stats.py](../../backend/app/api/stats.py).
- Session visibility and converted-drill boundary:
  [backend/app/session_contracts.py](../../backend/app/session_contracts.py).
- Cached session-accuracy lifecycle:
  [session accuracy versioning](../session-accuracy-versioning.md).
- Behavioral coverage:
  [backend/test_stats_api.py](../../backend/test_stats_api.py).
