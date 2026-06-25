import { memo } from "react";
import {
  formatScore,
  getGradeToken,
  getPriorityLabel,
} from "../../../openings/format";
import type { OpeningScoreDeltaItem } from "../../../utils/api";

type OpeningScoreDeltaProps = {
  changes?: OpeningScoreDeltaItem[] | null;
};

/**
 * Post-game/drill summary of how the played openings' scores moved (g-xanz),
 * broadest -> deepest. One row per crossed opening: "Italian Game 41 → 44 (+3) B".
 * A brand-new opening (no baseline entry) shows "new" with its after-score
 * instead of a numeric delta. Renders nothing when there are no changes (a game
 * that never left book, or the delta could not be computed). Shared by the
 * post-game banner (game + natural-ended drill) and the stopped-drill actions.
 */
const OpeningScoreDelta = ({ changes }: OpeningScoreDeltaProps) => {
  if (!changes || changes.length === 0) {
    return null;
  }

  return (
    <div
      className="opening-score-delta"
      role="group"
      aria-label="Opening score changes"
    >
      <p className="opening-score-delta__label">Opening scores</p>
      <ul className="opening-score-delta__list">
        {changes.map((item) => {
          const hasDelta = item.delta !== null;
          const direction = !hasDelta
            ? "flat"
            : (item.delta as number) >= 0
              ? "up"
              : "down";
          return (
            <li
              key={item.opening_key}
              className={`opening-score-delta__row opening-score-delta__row--${direction}`}
            >
              <span className="opening-score-delta__name">
                {item.opening_name}
              </span>
              <span className="opening-score-delta__scores">
                {item.is_new ? (
                  <span className="opening-score-delta__new">
                    new · {formatScore(item.after)}
                  </span>
                ) : (
                  <>
                    <span className="opening-score-delta__before">
                      {formatScore(item.before)}
                    </span>
                    <span
                      className="opening-score-delta__arrow"
                      aria-hidden="true"
                    >
                      →
                    </span>
                    <span className="opening-score-delta__after">
                      {formatScore(item.after)}
                    </span>
                    {hasDelta && (
                      <span className="opening-score-delta__change">
                        ({(item.delta as number) >= 0 ? "+" : ""}
                        {formatScore(item.delta)})
                      </span>
                    )}
                  </>
                )}
                {item.after !== null && (
                  <span
                    className={`opening-score-delta__grade opening-score-delta__grade--${getGradeToken(item.after)}`}
                    aria-label={`Grade ${getPriorityLabel(item.after)}`}
                  >
                    {getPriorityLabel(item.after)}
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
};

export default memo(OpeningScoreDelta);
