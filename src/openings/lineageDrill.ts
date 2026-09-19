import { Chess } from "chess.js";
import type { OpeningLineageItem } from "../utils/api";
import { normalize_fen } from "../utils/fen";
import type { DrillSelection } from "./drillSelection";

type LineageDrillRoute = { line: string[]; reason: null } | { line: null; reason: string };

/** The selected occurrence owns the prefix; registry depth/path never describe it. */
export function lineageDrillRoute(item: OpeningLineageItem, startPly: number): LineageDrillRoute {
  if (startPly !== 1) return { line: null, reason: "This route must begin at the starting position." };
  if (!item.moves.length) return { line: null, reason: "This card has no played route." };
  if (item.moves.length > 80) return { line: null, reason: "This route is too long to guide a drill." };
  const board = new Chess();
  const seen = new Set([normalize_fen(board.fen())]);
  const line: string[] = [];
  try {
    for (const san of item.moves) {
      const move = board.move(san);
      // chess.js accepts SAN "--" as a null move; a played route must be legal.
      if (move.from === move.to) return { line: null, reason: "This card's played route is unavailable." };
      line.push(move.from + move.to + (move.promotion ?? ""));
      const fen = normalize_fen(board.fen());
      if (seen.has(fen)) return { line: null, reason: "This route revisits a position and cannot guide a drill." };
      seen.add(fen);
    }
    if (normalize_fen(board.fen()) !== normalize_fen(item.opening_key)) {
      return { line: null, reason: "This route does not reach the card's opening." };
    }
    return { line, reason: null };
  } catch {
    return { line: null, reason: "This card's played route is unavailable." };
  }
}

export function lineageDrillSelection(item: OpeningLineageItem, line: string[]): DrillSelection {
  const { opening_key, opening_name, opening_family, eco, depth } = item;
  return {
    opening: { opening_key, opening_name, opening_family, eco, depth },
    line: [...line],
    routeMode: "prefer_line",
  };
}
