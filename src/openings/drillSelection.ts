import type { DrillRouteMode, OpeningRootItem } from "../utils/api";

export type DrillSelection = {
  opening: OpeningRootItem;
  line: string[] | null;
  routeMode: DrillRouteMode;
};

export type DrillSetupNavigation = { playerColor: string } & (
  | { selection: DrillSelection }
  | { openingKey: string }
  | { targetFen: string; line?: string[]; displayName?: string | null; eco?: string | null }
);

export function isCompleteDrillSelection(selection: DrillSelection | null): selection is DrillSelection {
  if (!selection) return false;
  const { opening, line, routeMode } = selection;
  return Boolean(opening.opening_key && opening.opening_name)
    && typeof opening.opening_family === "string"
    && (opening.eco === null || typeof opening.eco === "string")
    && Number.isFinite(opening.depth)
    && (routeMode === "auto" || (routeMode === "prefer_line" && !!line?.length));
}

export function navigationDrillSelection(nav: DrillSetupNavigation | undefined): DrillSelection | null {
  if (!nav) return null;
  if ("selection" in nav) return isCompleteDrillSelection(nav.selection) ? nav.selection : null;
  if (!("targetFen" in nav)) return null;
  return {
    opening: {
      opening_key: nav.targetFen,
      opening_name: nav.displayName ?? "Custom line",
      opening_family: "",
      eco: nav.eco ?? null,
      depth: nav.line?.length ?? 0,
    },
    line: nav.line ?? [],
    routeMode: "auto",
  };
}

type StoredDrillSelection = {
  drillOpeningKey: string | null;
  drillOpeningName: string | null;
  drillOpeningMetadata: Pick<OpeningRootItem, "opening_family" | "eco" | "depth"> | null;
  drillLine: string[] | null;
  drillRouteMode: DrillRouteMode;
};

export function storedDrillSelection(store: StoredDrillSelection): DrillSelection | null {
  if (!store.drillOpeningKey || !store.drillOpeningName || !store.drillOpeningMetadata) return null;
  return {
    opening: {
      opening_key: store.drillOpeningKey,
      opening_name: store.drillOpeningName,
      ...store.drillOpeningMetadata,
    },
    line: store.drillLine,
    routeMode: store.drillRouteMode,
  };
}
