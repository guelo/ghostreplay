import { describe, expect, it } from "vitest";
import {
  REASON_LABELS,
  deriveEndGameAnnouncement,
  type GameEndReason,
  type GameResult,
} from "./status";

describe("deriveEndGameAnnouncement", () => {
  it("maps a checkmate win to Victory with the tagged reason", () => {
    const result: GameResult = {
      type: "checkmate_win",
      message: "Checkmate! You won!",
      reason: "checkmate",
    };
    expect(deriveEndGameAnnouncement(result)).toEqual({
      outcome: "win",
      headline: "Victory",
      reason: "Checkmate",
    });
  });

  it("maps a checkmate loss to Defeat", () => {
    const result: GameResult = {
      type: "checkmate_loss",
      message: "Checkmate! You lost.",
      reason: "checkmate",
    };
    expect(deriveEndGameAnnouncement(result)).toMatchObject({
      outcome: "loss",
      headline: "Defeat",
      reason: "Checkmate",
    });
  });

  it("maps a resignation to Defeat", () => {
    const result: GameResult = {
      type: "resign",
      message: "You resigned.",
      reason: "resignation",
    };
    expect(deriveEndGameAnnouncement(result)).toMatchObject({
      outcome: "loss",
      headline: "Defeat",
      reason: "Resignation",
    });
  });

  it.each([
    ["stalemate", "Stalemate"],
    ["threefold", "Threefold repetition"],
    ["insufficient", "Insufficient material"],
    ["fifty_move", "Fifty-move rule"],
    ["draw", "Draw"],
  ] as const)("maps a %s draw to Draw with its label", (reason, label) => {
    const result: GameResult = { type: "draw", message: "x", reason };
    expect(deriveEndGameAnnouncement(result)).toEqual({
      outcome: "draw",
      headline: "Draw",
      reason: label,
    });
  });

  it("falls back to a type-derived reason when `reason` is omitted", () => {
    // Older/synthetic GameResults have no reason; the default keeps the subtitle
    // sensible rather than blank.
    expect(
      deriveEndGameAnnouncement({ type: "checkmate_win", message: "x" }).reason,
    ).toBe("Checkmate");
    expect(
      deriveEndGameAnnouncement({ type: "checkmate_loss", message: "x" }).reason,
    ).toBe("Checkmate");
    expect(
      deriveEndGameAnnouncement({ type: "resign", message: "x" }).reason,
    ).toBe("Resignation");
    expect(
      deriveEndGameAnnouncement({ type: "draw", message: "x" }).reason,
    ).toBe("Draw");
  });
});

describe("REASON_LABELS", () => {
  it("has a non-empty label for every GameEndReason", () => {
    const reasons: GameEndReason[] = [
      "checkmate",
      "stalemate",
      "threefold",
      "insufficient",
      "fifty_move",
      "draw",
      "resignation",
    ];
    for (const reason of reasons) {
      expect(REASON_LABELS[reason]).toBeTruthy();
    }
    // Completeness the other direction: no stray keys beyond the union.
    expect(Object.keys(REASON_LABELS).sort()).toEqual([...reasons].sort());
  });
});
