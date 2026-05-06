export const ANALYSIS_BOARD_DIAGNOSTICS_KEY =
  "ghostreplay:analysis-board-debug";

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);
const FALSE_VALUES = new Set(["0", "false", "no", "off"]);

export const isAnalysisBoardDiagnosticsEnabled = (): boolean => {
  if (typeof window === "undefined") return false;

  try {
    const params = new URLSearchParams(window.location.search);
    const queryValue =
      params.get("analysisBoardDebug") ?? params.get("analysisDebug");
    if (queryValue) {
      const normalized = queryValue.toLowerCase();
      if (TRUE_VALUES.has(normalized)) return true;
      if (FALSE_VALUES.has(normalized)) return false;
    }

    const storedValue = window.localStorage.getItem(
      ANALYSIS_BOARD_DIAGNOSTICS_KEY,
    );
    return storedValue ? TRUE_VALUES.has(storedValue.toLowerCase()) : false;
  } catch {
    return false;
  }
};

export const logAnalysisBoardDiagnostic = (
  label: string,
  data: Record<string, unknown>,
  level: "info" | "warn" = "info",
) => {
  console[level](`[AnalysisBoard perf] ${label}`, data);
};
