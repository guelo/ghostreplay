import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "../test/utils";
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
    expect(whiteIcon?.classList.contains("move-icon--celebrate-best")).toBe(false);
  });

  it("adds best-only celebration classes when freshBlack=true and classification is best", () => {
    const { container } = render(
      <MoveRow {...baseProps} freshBlack={true} />,
    );
    const blackButton = container.querySelector(".move-col-black.move-button");
    const blackIcon = container.querySelector(".move-col-black .move-icon");
    const blackSanText = container.querySelector(".move-col-black .move-san__text");
    const blackRing = container.querySelector(".move-col-black .move-icon-stage__ring");
    const blackConnector = container.querySelector(".move-col-black .move-san__connector");

    expect(blackButton?.classList.contains("move-button--celebrate-best")).toBe(true);
    expect(blackIcon?.classList.contains("move-icon--celebrate-best")).toBe(true);
    expect(blackSanText?.classList.contains("move-san__text--celebrate-best")).toBe(true);
    expect(blackRing).not.toBeNull();
    expect(blackConnector).not.toBeNull();
  });

  it("no pop class when fresh=false", () => {
    const { container } = render(
      <MoveRow {...baseProps} />,
    );
    const icons = container.querySelectorAll(".move-icon");
    for (const icon of icons) {
      expect(icon.classList.contains("move-icon--pop")).toBe(false);
      expect(icon.classList.contains("move-icon--celebrate-best")).toBe(false);
    }
  });

  it("calls onFreshAnimationDone on animationEnd", () => {
    const onDone = vi.fn();
    const { container } = render(
      <MoveRow {...baseProps} freshWhite={true} onFreshAnimationDone={onDone} />,
    );
    const icon = container.querySelector(".move-icon--pop");
    expect(icon).not.toBeNull();

    fireEvent.animationEnd(icon!);
    expect(onDone).toHaveBeenCalledWith(0); // whiteIdx
  });

  it("clears fresh best moves only after the final tail animation ends", () => {
    const onDone = vi.fn();
    const { container } = render(
      <MoveRow {...baseProps} freshBlack={true} onFreshAnimationDone={onDone} />,
    );

    const bestIcon = container.querySelector(".move-col-black .move-icon--celebrate-best");
    const bestBurst = container.querySelector(".move-col-black .move-icon-stage__burst");
    const bestTail = container.querySelector(".move-col-black .move-icon-stage__tail");

    expect(bestIcon).not.toBeNull();
    expect(bestBurst).not.toBeNull();
    expect(bestTail).not.toBeNull();

    fireEvent.animationEnd(bestIcon!);
    fireEvent.animationEnd(bestBurst!);
    expect(onDone).not.toHaveBeenCalled();

    fireEvent.animationEnd(bestTail!);
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(onDone).toHaveBeenCalledWith(1);
  });
});
