import { type RefObject } from "react";
import type { MoveMessage, SrsFailDetail } from "./MoveRow";

export type MoveMessagesProps = {
  msgs: MoveMessage[];
  moveIndex: number;
  /** Affects bubble arrow direction in the vertical list. */
  side?: "white" | "black";
  revealedSrsFailIndex: number | null;
  isInteractionDisabled?: boolean;
  onRevealSrsFail?: (detail: SrsFailDetail, moveIndex: number) => void;
  /** Attached to each bubble when set (vertical list auto-scroll target). */
  lastMessageRef?: RefObject<HTMLDivElement | null>;
};

/** Renders srs-pass / srs-fail bubble messages for a single move.
 *  Shared by the vertical MoveRow and the horizontal move list popup. */
const MoveMessages = ({
  msgs,
  moveIndex,
  side = "white",
  revealedSrsFailIndex,
  isInteractionDisabled = false,
  onRevealSrsFail,
  lastMessageRef,
}: MoveMessagesProps) => {
  const arrowClass = side === "black" ? "move-bubble--arrow-right" : "";
  return (
    <>
      {msgs.map((msg) => {
        const isRevealed = revealedSrsFailIndex === moveIndex;

        if (msg.variant === "srs-fail" && msg.srsFailDetail) {
          return (
            <div
              ref={lastMessageRef ?? null}
              key={msg.key}
              className={`move-bubble move-bubble--srs-fail ${arrowClass}`}
            >
              <button
                type="button"
                className={`srs-fail-icon ${isRevealed ? "srs-fail-icon--revealed" : ""}`}
                disabled={isInteractionDisabled}
                onClick={() => {
                  if (isInteractionDisabled) return;
                  if (!isRevealed && onRevealSrsFail && msg.srsFailDetail) {
                    onRevealSrsFail(msg.srsFailDetail, moveIndex);
                  }
                }}
                title="Click to see what you should have played"
              >
                <span className="srs-fail-icon__symbol">!</span>
              </button>
              <div className="srs-fail-body">
                <span className="srs-fail-body__label">{msg.text}</span>
                {msg.srsStats && (
                  <span className="srs-stats">
                    <span className="srs-stats__item srs-stats__item--pass">
                      <span className="srs-stats__value">
                        {msg.srsStats.passCount}
                      </span>
                      <span className="srs-stats__caption">pass</span>
                    </span>
                    <span className="srs-stats__item srs-stats__item--fail">
                      <span className="srs-stats__value">
                        {msg.srsStats.failCount}
                      </span>
                      <span className="srs-stats__caption">fail</span>
                    </span>
                    <span className="srs-stats__item srs-stats__item--streak">
                      <span className="srs-stats__value">
                        {msg.srsStats.streak}
                      </span>
                      <span className="srs-stats__caption">streak</span>
                    </span>
                  </span>
                )}
              </div>
            </div>
          );
        }

        return (
          <div
            ref={lastMessageRef ?? null}
            key={msg.key}
            className={`move-bubble move-bubble--${msg.variant} ${arrowClass}`}
          >
            <span>{msg.text}</span>
            {msg.srsStats && (
              <span className="srs-stats">
                <span className="srs-stats__item srs-stats__item--pass">
                  <span className="srs-stats__value">
                    {msg.srsStats.passCount}
                  </span>
                  <span className="srs-stats__caption">pass</span>
                </span>
                <span className="srs-stats__item srs-stats__item--fail">
                  <span className="srs-stats__value">
                    {msg.srsStats.failCount}
                  </span>
                  <span className="srs-stats__caption">fail</span>
                </span>
                <span className="srs-stats__item srs-stats__item--streak">
                  <span className="srs-stats__value">
                    {msg.srsStats.streak}
                  </span>
                  <span className="srs-stats__caption">streak</span>
                </span>
              </span>
            )}
          </div>
        );
      })}
    </>
  );
};

export default MoveMessages;
