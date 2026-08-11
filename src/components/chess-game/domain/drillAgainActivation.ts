export type DrillAgainInputMethod =
  | "pointer"
  | "keyboard"
  | "programmatic";

type ActivationMetadata = Pick<MouseEvent, "detail" | "isTrusted">;

/** Classify native button-activation metadata without treating element.click()
 * (an untrusted zero-detail event) as a keyboard action. */
export function classifyDrillAgainInput(
  event?: ActivationMetadata | null,
): DrillAgainInputMethod {
  if (!event) return "programmatic";
  if (event.detail === 0 && !event.isTrusted) return "programmatic";
  if (event.detail > 0) return "pointer";
  return "keyboard";
}
