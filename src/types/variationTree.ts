export type VariationNodeId = string

export type VarNode = {
  id: VariationNodeId
  san: string
  fen: string           // FEN after this move
  fenBefore: string     // FEN before this move (for square highlights)
  uci: string           // UCI notation (for analysis)
  parentId: VariationNodeId | null // null => parent is a game move
  parentGameIndex: number          // game moveIndex where this branch departs from main line
  branchPlyOffset: number          // 0-based ply within the immediate branch
  children: VariationNodeId[]      // [0] = continuation, [1..] = sub-branches
  nestingLevel: number  // 0 = branch off game, 1 = branch off branch, etc.
}

export type VariationTree = {
  nodes: Map<VariationNodeId, VarNode>
  // Root branches keyed by game moveIndex they depart from.
  // -1 is valid (branch from starting position).
  // Each value = array of first-ply node IDs, chronological order.
  rootBranches: Map<number, VariationNodeId[]>
}

export type ParentContext =
  | { type: 'game'; moveIndex: number }
  | { type: 'variation'; nodeId: VariationNodeId }

export const createEmptyTree = (): VariationTree => ({
  nodes: new Map(),
  rootBranches: new Map(),
})
