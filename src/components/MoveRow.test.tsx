import { describe, expect, it, vi } from "vitest";
import { render } from "../test/utils";
import MoveRow from "./MoveRow";
import type { MoveRowProps } from "./MoveRow";
import { createRef } from "react";

const baseProps: MoveRowProps = {
  pairNumber: 1,
  white: { san: "e4", classification: "good", eval: 30 },
  black: { san: "e5", classification: "best", eval: 20 },
  whiteIdx: 0,
  blackIdx: 1,
  prevWhiteEval: 0,
  prevBlackEval: 30,
  isWhiteSelected: false,
  isBlackSelected: false,
  whiteBubbles: [],
  blackBubbles: [],
  isLastBubbleRow: false,
  analyzingWhite: false,
  analyzingBlack: false,
  freshWhite: false,
  freshBlack: false,
  playerColor: "white",
  tappedIconIndex: null,
  revealedSrsFailIndex: null,
  onMoveClick: vi.fn(),
  onIconTap: vi.fn(),
  selectedMoveRef: createRef(),
  lastMessageRef: createRef(),
};

describe("MoveRow — pop animation classes", () => {
  it("adds move-icon--pop when freshWhite=true and classification is not best", () => {
    const { container } = render(
      <MoveRow {...baseProps} freshWhite={true} />,
    );
    const whiteIcon = container.querySelector(".move-col-white .move-icon");
    expect(whiteIcon?.classList.contains("move-icon--pop")).toBe(true);
    expect(whiteIcon?.classList.contains("move-icon--pop-best")).toBe(false);
  });

  it("adds move-icon--pop-best when freshBlack=true and classification is best", () => {
    const { container } = render(
      <MoveRow {...baseProps} freshBlack={true} />,
    );
    const blackIcon = container.querySelector(".move-col-black .move-icon");
    expect(blackIcon?.classList.contains("move-icon--pop-best")).toBe(true);
  });

  it("no pop class when fresh=false", () => {
    const { container } = render(
      <MoveRow {...baseProps} />,
    );
    const icons = container.querySelectorAll(".move-icon");
    for (const icon of icons) {
      expect(icon.classList.contains("move-icon--pop")).toBe(false);
      expect(icon.classList.contains("move-icon--pop-best")).toBe(false);
    }
  });

  it("calls onFreshAnimationDone on animationEnd", () => {
    const onDone = vi.fn();
    const { container } = render(
      <MoveRow {...baseProps} freshWhite={true} onFreshAnimationDone={onDone} />,
    );
    const icon = container.querySelector(".move-icon--pop");
    expect(icon).not.toBeNull();

    // Simulate animationEnd event
    const event = new Event("animationend", { bubbles: true });
    icon!.dispatchEvent(event);
    expect(onDone).toHaveBeenCalledWith(0); // whiteIdx
  });
});
