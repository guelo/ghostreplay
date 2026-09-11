import type {
  DrillRouteMetadata,
  SessionDecisionSource,
  TargetBlunderSrs,
} from "../../../utils/api";

export type OpponentMode = "ghost" | "engine";

export type CommittedOpponentDrill = {
  openingKey: string;
  state: "active" | "root_reached";
};

export type OpponentMoveCommitContext = {
  drill: CommittedOpponentDrill | null;
};

export type OpponentMoveCommittedObserver = (
  context: OpponentMoveCommitContext,
) => void;

export type ApplyOpponentMoveOutcome =
  | { committed: true }
  | {
      committed: false;
      reason:
        | "inactive"
        | "revert_pending"
        | "stale"
        | "no_move"
        | "illegal_move"
        | "application_error";
    };

export type LastCommittedOpponentDecision = {
  mode: OpponentMode;
  decisionSource: SessionDecisionSource;
  targetBlunderId: number | null;
  targetBlunderSrs: TargetBlunderSrs | null;
  targetFen: string | null;
  drillRoute: DrillRouteMetadata | null;
  decisionId: string | null;
  drill: CommittedOpponentDrill | null;
};

export type OpponentPresentation =
  | { kind: "engine" }
  | { kind: "opening_guide" }
  | {
      kind: "targeted_ghost";
      targetBlunderId: number;
      targetBlunderSrs: TargetBlunderSrs | null;
      targetFen: string | null;
    };

const ENGINE_PRESENTATION: OpponentPresentation = { kind: "engine" };
const OPENING_GUIDE_PRESENTATION: OpponentPresentation = {
  kind: "opening_guide",
};

/**
 * Classifies only committed opponent decisions. A concrete target id is the
 * sole source of Replay Ghost identity; live review/drill state must not alter
 * the label after the move has reached the board.
 */
export const deriveOpponentPresentation = (
  decision: LastCommittedOpponentDecision | null,
): OpponentPresentation => {
  if (!decision) {
    return ENGINE_PRESENTATION;
  }

  if (decision.mode === "ghost" && decision.targetBlunderId !== null) {
    return {
      kind: "targeted_ghost",
      targetBlunderId: decision.targetBlunderId,
      targetBlunderSrs: decision.targetBlunderSrs,
      targetFen: decision.targetFen,
    };
  }

  if (decision.mode === "ghost" && decision.drill !== null) {
    return OPENING_GUIDE_PRESENTATION;
  }

  return ENGINE_PRESENTATION;
};
