import { describe, expect, it } from "vitest";
import type { LastCommittedOpponentDecision } from "./opponentPresentation";
import { deriveOpponentPresentation } from "./opponentPresentation";

const decision = (
  overrides: Partial<LastCommittedOpponentDecision> = {},
): LastCommittedOpponentDecision => ({
  mode: "engine",
  decisionSource: "backend_engine",
  targetBlunderId: null,
  targetBlunderSrs: null,
  targetFen: null,
  drillRoute: null,
  decisionId: null,
  drill: null,
  ...overrides,
});

describe("deriveOpponentPresentation", () => {
  it("uses engine presentation for reset, backend engine, and local fallback", () => {
    expect(deriveOpponentPresentation(null)).toEqual({ kind: "engine" });
    expect(deriveOpponentPresentation(decision())).toEqual({ kind: "engine" });
    expect(
      deriveOpponentPresentation(
        decision({ decisionSource: "local_fallback" }),
      ),
    ).toEqual({ kind: "engine" });
  });

  it("makes an id-bearing Ghost decision targeted even without optional details", () => {
    expect(
      deriveOpponentPresentation(
        decision({
          mode: "ghost",
          decisionSource: "ghost_path",
          targetBlunderId: 42,
        }),
      ),
    ).toEqual({
      kind: "targeted_ghost",
      targetBlunderId: 42,
      targetBlunderSrs: null,
      targetFen: null,
    });
  });

  it("uses Opening Guide for targetless Ghost moves committed during a drill", () => {
    expect(
      deriveOpponentPresentation(
        decision({
          mode: "ghost",
          decisionSource: "ghost_path",
          drill: { openingKey: "sicilian", state: "root_reached" },
        }),
      ),
    ).toEqual({ kind: "opening_guide" });
  });

  it("classifies targetless Ghost moves outside a drill as engine", () => {
    expect(
      deriveOpponentPresentation(
        decision({ mode: "ghost", decisionSource: "ghost_path" }),
      ),
    ).toEqual({ kind: "engine" });
  });

  it("does not infer a Replay Ghost target from the pre-root route FEN", () => {
    expect(
      deriveOpponentPresentation(
        decision({
          mode: "ghost",
          decisionSource: "ghost_path",
          targetFen: "root-fen",
          drillRoute: {
            status: "root_pending",
            target_fen: "root-fen",
            resulting_fen: "root-fen",
            plies_to_target: 0,
            reaches_root: true,
          },
          drill: { openingKey: "root-fen", state: "active" },
        }),
      ),
    ).toEqual({ kind: "opening_guide" });
  });
});
