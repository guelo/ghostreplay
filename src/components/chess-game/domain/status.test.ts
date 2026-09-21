import { describe, expect, it } from "vitest";
import {
  REASON_LABELS,
  deriveDrillStopAnnouncement,
  deriveDrillStopBanner,
  deriveEndGameAnnouncement,
  drillStopEngineMessage,
  type DrillTerminalReason,
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

describe("deriveDrillStopAnnouncement", () => {
  it("announces an accuracy stop with the drill-stop tone", () => {
    expect(deriveDrillStopAnnouncement("accuracy")).toEqual({
      outcome: "drill-stop",
      headline: "Bad move",
      reason: "Too inaccurate",
    });
  });

  it("announces an off-route stop", () => {
    expect(deriveDrillStopAnnouncement("off_route")).toEqual({
      outcome: "drill-stop",
      headline: "Off route",
      reason: "Left the route",
    });
  });

  it("announces nothing for a natural end (the real game-end fanfare covers it)", () => {
    expect(deriveDrillStopAnnouncement("natural_end")).toBeNull();
  });

  it("announces nothing for an untagged stop", () => {
    expect(deriveDrillStopAnnouncement(null)).toBeNull();
  });
});

describe("deriveDrillStopBanner", () => {
  it("returns the fail variant with a detail line for an accuracy stop", () => {
    expect(deriveDrillStopBanner("accuracy")).toEqual({
      variant: "fail",
      headline: "Bad move",
      detail: "That move exceeded this drill's centipawn limit",
    });
  });

  it("returns the fail variant with a detail line for an off-route stop", () => {
    expect(deriveDrillStopBanner("off_route")).toEqual({
      variant: "fail",
      headline: "Off route",
      detail: "That's not how you get to the opening",
    });
  });

  it.each<DrillTerminalReason>(["natural_end", null])(
    "returns the neutral variant with no detail for %s",
    (reason) => {
      expect(deriveDrillStopBanner(reason)).toEqual({
        variant: "neutral",
        headline: "Drill stopped.",
        detail: null,
      });
    },
  );
});

describe("drill stop copy", () => {
  // The fanfare and the panel banner are on screen at the same time (~2.85s),
  // so a shared string would make every getByText for it ambiguous.
  it.each<DrillTerminalReason>(["accuracy", "off_route"])(
    "never renders the same string on the card and in the panel for %s",
    (reason) => {
      const announcement = deriveDrillStopAnnouncement(reason);
      const banner = deriveDrillStopBanner(reason);
      expect(announcement).not.toBeNull();
      expect(announcement!.reason).not.toBe(banner.detail);
    },
  );

  it("sources the engine message from the banner detail", () => {
    expect(drillStopEngineMessage("accuracy")).toBe(
      deriveDrillStopBanner("accuracy").detail,
    );
    expect(drillStopEngineMessage("off_route")).toBe(
      deriveDrillStopBanner("off_route").detail,
    );
  });

  it("falls back to the neutral headline when the stop has no reason", () => {
    expect(drillStopEngineMessage("natural_end")).toBe(
      deriveDrillStopBanner("natural_end").headline,
    );
    expect(drillStopEngineMessage(null)).toBe(
      deriveDrillStopBanner(null).headline,
    );
  });
});
