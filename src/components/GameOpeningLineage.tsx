import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import OpeningTreeNodeCard, {
  type OpeningTreeNodeView,
} from "./OpeningTreeNodeCard";
import { buildOpeningsSearchParams } from "../openings/route";
import type {
  OpeningLineageItem,
  OpeningPlayerColor,
  OpeningScoreDeltaItem,
  OpeningScoreStatus,
} from "../utils/api";
// Shared with the last-drill toast (g-f3m4) so both surfaces agree on exactly
// what counts as a change.
import { badgeFor } from "../utils/openingDeltaBadge";

interface GameOpeningLineageProps {
  playerColor: OpeningPlayerColor;
  lineage: OpeningLineageItem[];
  /** Ply of each item's `moves[0]` (1 = White's move 1); anchors move-list
   *  numbering on the cards. From the session-openings response. */
  startPly: number;
  /** Post-game/drill opening-score changes (g-xanz), keyed by opening_key. When
   *  provided, a changed opening shows an inline diff badge to the right of its
   *  card (g-3gmc). Null during live play -> no badges. */
  scoreChanges?: OpeningScoreDeltaItem[] | null;
  /** When provided, tapping a chip selects that opening's root on the
   *  board/MoveList/graph (history parity). Omit for the live game panel, where
   *  cards are expand-only and must NOT move the live board. */
  onSelectRoot?: (item: OpeningLineageItem) => void;
  /** When provided, the expanded card shows a Start Drill button. Omit to hide
   *  it (regular/converted live game panel). */
  onStartDrill?: (item: OpeningLineageItem) => void;
  /** Whether the server's opening scores are still being computed (g-a5v3).
   *  "pending" makes each card render a loading placeholder in place of its
   *  score, so a cold cache reads as "loading" rather than "unscored".
   *  Defaults to "ready" — callers that never see a cold cache can omit it. */
  scoreStatus?: OpeningScoreStatus;
  /** Indices of live, locally-derived card occurrences that have not received
   *  a matching server lineage row yet. Kept separate from scoreStatus because
   *  a warm score cache can be ready while the current move is still uploading. */
  pendingScoreIndices?: ReadonlySet<number>;
  /** Index of the move the main board is displaying (g-m1xc), used to keep the
   *  expanded card in sync with the board. `-1` is the starting position;
   *  `null` means the board is off the played main line (an analysis
   *  variation), which collapses every card. Omit entirely to opt out of
   *  synchronization and keep the fully manual expand/collapse behavior. */
  activeMoveIndex?: number | null;
}

/**
 * Occurrence key of the card the board is inside (g-m1xc), or null when no card
 * describes the displayed position.
 *
 * The match is the LAST crossing whose crossing move is at or before the
 * displayed move. `moves` is the played SAN prefix up to and including the
 * crossing move, so its last index is that move's index — the same per-crossing
 * index card-to-board navigation uses. Lineage order (not `OpeningRoot.depth`)
 * is the authoritative played order.
 *
 * A card holds the expansion until the next crossing takes over, so a position
 * between two roots keeps the most recently crossed opening open. The deepest
 * crossing has no successor to hand off to, and so takes the only bound that
 * exists — its own crossing move: once the board moves past it the game has left
 * the opening and NO card is expanded.
 */
function matchCard(
  lineage: OpeningLineageItem[],
  activeMoveIndex: number,
): string | null {
  let matched: { item: OpeningLineageItem; index: number } | null = null;
  let deepest: { item: OpeningLineageItem; index: number } | null = null;
  for (let index = 0; index < lineage.length; index += 1) {
    const item = lineage[index];
    // An empty prefix has no resolvable crossing index — never auto-expanded.
    if (item.moves.length === 0) continue;
    deepest = { item, index };
    if (item.moves.length - 1 <= activeMoveIndex) matched = { item, index };
  }
  if (!matched) return null;
  const isDeepest = matched.index === deepest?.index;
  if (isDeepest && activeMoveIndex > matched.item.moves.length - 1) return null;
  return `${matched.item.opening_key}:${matched.index}`;
}

/**
 * Map a lineage item (an opening family identified by a position) onto the
 * tree-node card's view-model. A family has no SAN / ply / eval, so those
 * move-only fields are nulled out and the card is rendered with `kind="family"`
 * (name as header, played move list as the secondary line, no Eval tile /
 * move-type chips). `depth` feeds `ply` for completeness only — family mode
 * never reads it. `moveListSan` is the player's actual SAN prefix, numbered from
 * `startPly`. `score` is the resolved card score — usually `item.score`, but
 * pinned to the delta's pre-game `before` at game end (g-gkkn).
 */
function toNodeView(
  item: OpeningLineageItem,
  startPly: number,
  score: number | null,
): OpeningTreeNodeView {
  return {
    ply: item.depth,
    san: null,
    openingName: item.opening_name,
    eco: item.eco,
    inBook: true,
    isUserSelected: false,
    isTransposition: false,
    score,
    evalCp: null,
    evalMate: null,
    coverage: item.coverage,
    gameCount: item.game_count,
    isTerminal: false,
    terminalReason: null,
    drillOpeningKey: item.opening_key,
    moveListSan: item.moves,
    moveListStartPly: startPly,
  };
}

/**
 * Compact vertical stack of opening cards (broadest -> deepest), shared by the
 * /history analysis footer and the live game chess-panel. Each entry is a
 * compact `OpeningTreeNodeCard` (family mode) that expands in place to the
 * expanded variant — the same card /openings uses, minus the mini board. When
 * `onSelectRoot` is provided, tapping a card ALSO selects that opening's root on
 * the board/MoveList/graph (history); when omitted the card is expand-only (live
 * panel). "View in Openings" and the optional Start Drill button share the
 * expanded card's generic footer-action channel.
 *
 * With `activeMoveIndex` supplied the stack also follows the board (g-m1xc): the
 * card the board is inside (see `matchCard`) is expanded automatically, so the
 * card on screen always describes the position on the board — and once the board
 * leaves the opening, no card is expanded.
 */
function GameOpeningLineage({
  playerColor,
  lineage,
  startPly,
  scoreChanges,
  onSelectRoot,
  onStartDrill,
  scoreStatus = "ready",
  pendingScoreIndices,
  activeMoveIndex,
}: GameOpeningLineageProps) {
  // A manual expand/collapse, stamped with the synchronization state it was made
  // against (see `syncToken`). Cards are addressed by a per-occurrence key
  // (opening_key + index), not the bare opening_key: a lineage can (defensively)
  // repeat the same root as separate crossings, and keying by opening_key alone
  // would collide React keys and expand every matching card at once.
  const [manual, setManual] = useState<{
    token: string;
    key: string | null;
  } | null>(null);

  const changeByKey = useMemo(
    () => new Map((scoreChanges ?? []).map((c) => [c.opening_key, c])),
    [scoreChanges],
  );

  const matchedKey = useMemo(
    () =>
      activeMoveIndex === undefined || activeMoveIndex === null
        ? null
        : matchCard(lineage, activeMoveIndex),
    [lineage, activeMoveIndex],
  );

  // Identity of the current synchronization state, and the lifetime of a manual
  // choice: the manual key wins only while the board has not moved and the
  // matched card has not changed. Deliberately NOT the whole lineage — a
  // score-only hydration must not blow away what the player just opened, while a
  // lineage arriving after the board settled DOES change `matchedKey` and so
  // opens the right card without waiting for another board move. With
  // `activeMoveIndex` omitted the token is constant and `matchedKey` is null, so
  // the manual choice never expires — the original fully manual behavior.
  const syncToken = `${String(activeMoveIndex)}|${matchedKey ?? ""}`;
  // The override is DISCARDED on the first token change, not merely ignored
  // while the token differs: tokens recur (rewinding to a move already visited
  // rebuilds that move's token), and a collapse made there must not come back to
  // life on the return visit. Resetting state during render is React's sanctioned
  // alternative to a state-syncing effect — this render's output is discarded and
  // re-run with `manual` already null, so `override` below stands in for it.
  let override = manual;
  if (manual && manual.token !== syncToken) {
    setManual(null);
    override = null;
  }
  const expandedKey = override ? override.key : matchedKey;

  if (lineage.length === 0) {
    return null;
  }

  return (
    <section className="game-opening-lineage" aria-label="Openings played">
      <p className="game-opening-lineage__label">Openings</p>
      <ol className="game-opening-lineage__list">
        {lineage.map((item, index) => {
          const cardKey = `${item.opening_key}:${index}`;
          const isExpanded = expandedKey === cardKey;
          const cardId = `opening-card-${index}`;
          const openingsHref = `/openings?${buildOpeningsSearchParams({
            playerColor,
            opening: item.opening_key,
          })}`;
          const change = changeByKey.get(item.opening_key);
          const badge = badgeFor(change);
          const badgeSign = badge && badge.diff > 0 ? "+" : "";
          // Pre-game score pin (g-gkkn): a known `before` overrides the
          // refetched post-game item.score, while a truly new opening keeps
          // "—" because it has no baseline. For a non-new opening whose baseline
          // is unavailable or incompatible, use the delta's available `after`
          // instead of a stale lineage score. That branch deliberately applies
          // to both the warm terminal envelope and its reconciled replacement;
          // during the warm phase its `after` agrees with the same persisted
          // batch read by the lineage refetch. item.score remains the defensive
          // fallback only when neither delta value is available.
          const cardScore = !change
            ? item.score
            : change.is_new
              ? null
              : change.before ?? change.after ?? item.score;
          // A settled delta display wins over the pending spinner: once `change`
          // is present the card shows the selected baseline/after value, and a
          // spinner would hide the number that the terminal state established.
          const scorePending =
            !change &&
            (scoreStatus === "pending" || pendingScoreIndices?.has(index) === true);
          const view = toNodeView(item, startPly, cardScore);

          return (
            <li
              key={cardKey}
              className="game-opening-lineage__item"
              style={{ "--lineage-depth": index } as React.CSSProperties}
            >
              {isExpanded ? (
                // Expanded card replaces the collapsed one; its full-surface
                // overlay button collapses it. Both caller-owned actions share
                // footerAction, where they stay above and independent from the
                // collapse surface. This component owns the router Link so the
                // card stays router-free.
                <div className="opening-lineage-card" id={cardId}>
                  <OpeningTreeNodeCard
                    variant="expanded"
                    kind="family"
                    node={view}
                    scorePending={scorePending}
                    onCollapse={() => setManual({ token: syncToken, key: null })}
                    footerAction={
                      <>
                        {onStartDrill && (
                          <button
                            type="button"
                            className="tree-node-card__action-button"
                            onClick={() => onStartDrill(item)}
                          >
                            Start Drill
                          </button>
                        )}
                        <Link
                          className="opening-lineage-card__openings-link"
                          to={openingsHref}
                        >
                          View in Openings
                        </Link>
                      </>
                    }
                  />
                </div>
              ) : (
                <OpeningTreeNodeCard
                  variant="compact"
                  kind="family"
                  node={view}
                  scorePending={scorePending}
                  // Wording reflects the action: history selects a root + toggles
                  // details; the live panel only expands the card in place.
                  ariaLabel={
                    onSelectRoot
                      ? `Select ${item.opening_name} and toggle details`
                      : `Show ${item.opening_name} details`
                  }
                  onSelect={() => {
                    setManual({ token: syncToken, key: cardKey });
                    onSelectRoot?.(item);
                  }}
                  isSelected={isExpanded}
                  isExpanded={isExpanded}
                  controlsId={cardId}
                />
              )}
              {/* Inline score-diff badge (g-3gmc): sibling of the card, to its
                  right, shown in both collapsed and expanded states. */}
              {badge && (
                <span
                  className={`game-opening-lineage__delta game-opening-lineage__delta--${badge.dir}`}
                  aria-label={`${item.opening_name} score ${badgeSign}${badge.diff}, now ${badge.after}`}
                >
                  {badgeSign}
                  {badge.diff} → {badge.after}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export default GameOpeningLineage;
