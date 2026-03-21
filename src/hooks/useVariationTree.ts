import { useCallback, useRef, useState } from 'react'
import type {
  VariationNodeId,
  VarNode,
  VariationTree,
  ParentContext,
} from '../types/variationTree'
import { createEmptyTree } from '../types/variationTree'
import type { AnalysisResult } from './useMoveAnalysis'

const createNodeId = (): VariationNodeId => {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }
  return Math.random().toString(36).slice(2)
}

export type AddMoveParams = {
  san: string
  fen: string
  fenBefore: string
  uci: string
  parentContext: ParentContext
}

/**
 * Result of navigateUp: either back to a variation node or to a game move.
 */
export type NavigateUpResult =
  | { type: 'variation'; nodeId: VariationNodeId }
  | { type: 'game'; moveIndex: number }

export const useVariationTree = () => {
  // Use ref as source of truth for synchronous reads + a version counter for re-renders
  const treeRef = useRef<VariationTree>(createEmptyTree())
  const [, setVersion] = useState(0)
  const [selectedVarNodeId, setSelectedVarNodeId] = useState<VariationNodeId | null>(null)

  // FEN-keyed analysis cache for variation positions
  const varAnalysisCacheRef = useRef<Map<string, AnalysisResult>>(new Map())
  // Request ID → FEN mapping for in-flight analysis
  const pendingRequestsRef = useRef<Map<string, string>>(new Map())

  const triggerRender = useCallback(() => setVersion(v => v + 1), [])

  /**
   * Add a move to the variation tree. Returns the node ID of the added (or existing) node.
   * Deduplication: if a child with the same SAN already exists, returns that child's ID.
   */
  const addMove = useCallback(({ san, fen, fenBefore, uci, parentContext }: AddMoveParams): VariationNodeId => {
    const tree = treeRef.current

    if (parentContext.type === 'game') {
      const { moveIndex } = parentContext

      // Check for dedup among existing root branches at this moveIndex
      const existing = tree.rootBranches.get(moveIndex) ?? []
      for (const rootId of existing) {
        const rootNode = tree.nodes.get(rootId)
        if (rootNode && rootNode.san === san) {
          return rootId // dedup — no mutation
        }
      }

      // Create new root branch node
      const id = createNodeId()
      const node: VarNode = {
        id,
        san,
        fen,
        fenBefore,
        uci,
        parentId: null,
        parentGameIndex: moveIndex,
        branchPlyOffset: 0,
        children: [],
        nestingLevel: 0,
      }
      tree.nodes.set(id, node)
      tree.rootBranches.set(moveIndex, [...existing, id])
      triggerRender()
      return id
    }

    // parentContext.type === 'variation'
    const { nodeId: parentNodeId } = parentContext
    const parentNode = tree.nodes.get(parentNodeId)
    if (!parentNode) return ''

    // Check for dedup among all children
    for (const childId of parentNode.children) {
      const child = tree.nodes.get(childId)
      if (child && child.san === san) {
        return childId // dedup — no mutation
      }
    }

    // Create new child node
    const id = createNodeId()
    const isFirstChild = parentNode.children.length === 0
    const node: VarNode = {
      id,
      san,
      fen,
      fenBefore,
      uci,
      parentId: parentNodeId,
      parentGameIndex: parentNode.parentGameIndex,
      branchPlyOffset: isFirstChild ? parentNode.branchPlyOffset + 1 : 0,
      children: [],
      nestingLevel: isFirstChild ? parentNode.nestingLevel : parentNode.nestingLevel + 1,
    }
    tree.nodes.set(id, node)

    // Update parent's children (mutate in place since we own the ref)
    parentNode.children = [...parentNode.children, id]

    triggerRender()
    return id
  }, [triggerRender])

  /**
   * Navigate up from a node: returns parent variation node or game move.
   */
  const navigateUp = useCallback((nodeId: VariationNodeId): NavigateUpResult | null => {
    const node = treeRef.current.nodes.get(nodeId)
    if (!node) return null

    if (node.parentId) {
      return { type: 'variation', nodeId: node.parentId }
    }
    return { type: 'game', moveIndex: node.parentGameIndex }
  }, [])

  /**
   * Navigate down from a node: returns first child's ID or null (dead end).
   */
  const navigateDown = useCallback((nodeId: VariationNodeId): VariationNodeId | null => {
    const node = treeRef.current.nodes.get(nodeId)
    if (!node || node.children.length === 0) return null
    return node.children[0]
  }, [])

  /**
   * Get the absolute ply index of a node by walking the parent chain.
   * Absolute ply = parentGameIndex + 1 + depth from variation root.
   */
  const getAbsolutePly = useCallback((nodeId: VariationNodeId): number => {
    const tree = treeRef.current
    const node = tree.nodes.get(nodeId)
    if (!node) return 0

    let depth = 0
    let current: VarNode | undefined = node
    while (current && current.parentId) {
      depth++
      current = tree.nodes.get(current.parentId)
    }
    // current is now the root of this branch
    const gameIndex = current?.parentGameIndex ?? node.parentGameIndex
    return gameIndex + 1 + depth
  }, [])

  /**
   * Collect all nodes in a branch following children[0] continuation.
   */
  const collectBranchNodes = useCallback((rootNodeId: VariationNodeId): VarNode[] => {
    const tree = treeRef.current
    const result: VarNode[] = []
    let currentId: VariationNodeId | null = rootNodeId
    while (currentId) {
      const node = tree.nodes.get(currentId)
      if (!node) break
      result.push(node)
      currentId = node.children.length > 0 ? node.children[0] : null
    }
    return result
  }, [])

  // Analysis cache methods
  const registerPending = useCallback((requestId: string, fen: string) => {
    pendingRequestsRef.current.set(requestId, fen)
  }, [])

  const resolvePending = useCallback((requestId: string, result: AnalysisResult) => {
    const fen = pendingRequestsRef.current.get(requestId)
    if (fen) {
      varAnalysisCacheRef.current.set(fen, result)
      pendingRequestsRef.current.delete(requestId)
    }
  }, [])

  const getVarAnalysis = useCallback((fen: string): AnalysisResult | undefined => {
    return varAnalysisCacheRef.current.get(fen)
  }, [])

  const clearTree = useCallback(() => {
    treeRef.current = createEmptyTree()
    setSelectedVarNodeId(null)
    varAnalysisCacheRef.current.clear()
    pendingRequestsRef.current.clear()
    triggerRender()
  }, [triggerRender])

  return {
    tree: treeRef.current,
    selectedVarNodeId,
    setSelectedVarNode: setSelectedVarNodeId,
    addMove,
    navigateUp,
    navigateDown,
    getAbsolutePly,
    collectBranchNodes,
    getVarAnalysis,
    registerPending,
    resolvePending,
    clearTree,
    // Expose refs for direct access in effects
    varAnalysisCacheRef,
    pendingRequestsRef,
  }
}
