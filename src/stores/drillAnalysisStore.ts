import { create } from "zustand";
import type { DrillAnalysisSnapshot } from "../components/chess-game/domain/sessionUpload";

/**
 * Narrow, non-persisted singleton store holding the transient drill-analysis
 * snapshot. Refreshing the page loses it by design (g-a406) — the review is
 * ephemeral until a dedicated drill-analysis endpoint exists.
 */
type DrillAnalysisStore = {
  snapshot: DrillAnalysisSnapshot | null;
  setSnapshot: (snapshot: DrillAnalysisSnapshot) => void;
  clear: () => void;
};

export const useDrillAnalysisStore = create<DrillAnalysisStore>((set) => ({
  snapshot: null,
  setSnapshot: (snapshot) => set({ snapshot }),
  clear: () => set({ snapshot: null }),
}));
