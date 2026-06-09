// Red → dark-gray ("black" midpoint) → green gradient for stat values shown on
// the white history stats pane. Anchors are chosen so every interpolated color
// clears WCAG-AA 4.5:1 contrast against white (#fff) for ~15px text.
const RED = [183, 28, 28]; // ~5.9:1 on white
const MID = [33, 33, 33]; // ~12.6:1 — the dark "black" midpoint
const GREEN = [27, 122, 50]; // ~4.8:1 on white

function lerp(a: number[], b: number[], t: number): string {
  const ch = (i: number) => Math.round(a[i] + (b[i] - a[i]) * t);
  return `rgb(${ch(0)}, ${ch(1)}, ${ch(2)})`;
}

/** t clamped to [0,1]; 0 = red, 0.5 = dark midpoint, 1 = green. */
export function gradientColor(t: number): string {
  const c = Math.max(0, Math.min(1, t));
  return c < 0.5 ? lerp(RED, MID, c * 2) : lerp(MID, GREEN, (c - 0.5) * 2);
}

/** Accuracy is higher-is-better: 60% → red, 80% → mid, 100% → green. */
export function accuracyColor(pct: number): string {
  return gradientColor((pct - 60) / 40);
}

/** ACPL is lower-is-better: 100 → red, 50 → mid, 0 → green. */
export function acplColor(cpl: number): string {
  return gradientColor((100 - cpl) / 100);
}
