import { describe, expect, it } from "vitest";
import { render } from "../../../test/utils";
import StaticMiniBoard from "./StaticMiniBoard";

describe("StaticMiniBoard", () => {
  it("renders 64 squares and forwards position/orientation", () => {
    const { getByTestId, container } = render(
      <StaticMiniBoard fen="8/8/8/8/8/8/8/8 w - - 0 1" />,
    );
    const board = getByTestId("ghost-board");
    expect(board).toHaveAttribute("data-position", "8/8/8/8/8/8/8/8 w - - 0 1");
    expect(board).toHaveAttribute("data-orientation", "white");
    expect(container.querySelectorAll(".static-mini-board__sq")).toHaveLength(64);
  });

  it("places pieces from the FEN placement field", () => {
    const { container } = render(
      <StaticMiniBoard fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w" />,
    );
    expect(container.querySelectorAll(".static-mini-board__piece")).toHaveLength(
      32,
    );
    expect(
      container.querySelectorAll(".static-mini-board__piece--white"),
    ).toHaveLength(16);
    expect(
      container.querySelectorAll(".static-mini-board__piece--black"),
    ).toHaveLength(16);
  });

  it("flips square layout when oriented black", () => {
    const { getByTestId } = render(
      <StaticMiniBoard fen="8/8/8/8/8/8/8/8 w" orientation="black" />,
    );
    expect(getByTestId("ghost-board")).toHaveAttribute(
      "data-orientation",
      "black",
    );
  });

  it("flips piece and square order between orientations", () => {
    // White queen on h8, black king on a1 — opposite corners with distinct glyphs.
    const fen = "7Q/8/8/8/8/8/8/k7 w";
    const indexOf = (glyph: string, orientation: "white" | "black") => {
      const { container } = render(
        <StaticMiniBoard fen={fen} orientation={orientation} />,
      );
      const squares = Array.from(
        container.querySelectorAll(".static-mini-board__sq"),
      );
      return squares.findIndex((sq) => sq.textContent?.includes(glyph));
    };

    const queen = "♛";
    const king = "♚";

    // White's view: h8 is top-right (index 7), a1 is bottom-left (index 56).
    expect(indexOf(queen, "white")).toBe(7);
    expect(indexOf(king, "white")).toBe(56);

    // Black's view: board flips both axes, so the corners swap.
    expect(indexOf(queen, "black")).toBe(56);
    expect(indexOf(king, "black")).toBe(7);
  });
});
