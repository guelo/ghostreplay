export function strictnessFromCp(cp: number): "strict" | "standard" | "lenient" {
  if (cp <= 15) return "strict";
  if (cp <= 35) return "standard";
  return "lenient";
}

// UI presentation grouping over cp — the wire `strictness` tier is still derived
// from cp via strictnessFromCp at commit time. Band edges (15/35) must stay in
// sync with strictnessFromCp above and the backend tier-fallback thresholds.
export const STRICTNESS_TIERS = [
  {
    tier: "strict",
    label: "Strict",
    min: 0,
    max: 15,
    seedCp: 0,
    blurb: "Ends on the slightest inaccuracy — perfect play.",
  },
  {
    tier: "standard",
    label: "Standard",
    min: 16,
    max: 35,
    seedCp: 25,
    blurb: "Ends on a clear mistake.",
  },
  {
    tier: "lenient",
    label: "Lenient",
    min: 36,
    max: 50,
    seedCp: 50,
    blurb: "Ends only on a real blunder.",
  },
] as const;

export function bandForCp(cp: number) {
  return STRICTNESS_TIERS.find((t) => t.tier === strictnessFromCp(cp))!;
}

// Kept to one short line: the label renders in a ~180px column on mobile, so
// anything past ~24 chars wraps and pushes the fine-tune slider out of view.
export function strictnessStopCopy(cp: number): string {
  // cp=0 has exact-best semantics: non-best moves fail even when eval noise
  // resolves to 0cp loss, so "loses more than 0 cp" would be wrong.
  if (cp === 0) {
    return "Best move only";
  }
  return `Ends at −${cp} cp`;
}
