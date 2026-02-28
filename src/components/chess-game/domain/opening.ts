import type { OpeningLookupResult } from "../../../openings/openingBook";

export const deriveDisplayedOpening = (
  history: (OpeningLookupResult | null)[],
  viewIndex: number | null,
): OpeningLookupResult | null => {
  // history[0] = starting position, history[N] = after move N
  // viewIndex null = live, viewIndex -1 = starting position, viewIndex N = after move N
  const idx = viewIndex === null
    ? history.length - 1
    : viewIndex === -1
      ? 0
      : viewIndex + 1;

  // Walk backwards to find the last known opening at or before this index.
  for (let i = Math.min(idx, history.length - 1); i >= 0; i -= 1) {
    if (history[i]) return history[i];
  }
  return null;
};
