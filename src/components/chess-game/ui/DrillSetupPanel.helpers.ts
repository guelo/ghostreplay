export function strictnessFromCp(cp: number): "strict" | "standard" | "lenient" {
  if (cp <= 15) return "strict";
  if (cp <= 35) return "standard";
  return "lenient";
}
