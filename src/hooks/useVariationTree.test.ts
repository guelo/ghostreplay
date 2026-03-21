import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useVariationTree } from './useVariationTree'
import type { AnalysisResult } from './useMoveAnalysis'

const makeResult = (id: string, fen: string): AnalysisResult => ({
  id,
  move: 'e2e4',
  bestMove: 'e2e4',
  bestEval: 0,
  playedEval: 0,
  currentPositionEval: 0,
  moveIndex: null,
  delta: 0,
  classification: null,
  blunder: false,
  recordable: false,
})

describe('useVariationTree', () => {
  describe('addMove', () => {
    it('adds a root branch from a game move', () => {
      const { result } = renderHook(() => useVariationTree())

      let nodeId: string
      act(() => {
        nodeId = result.current.addMove({
          san: 'c5',
          fen: 'fen-after-c5',
          fenBefore: 'fen-before-c5',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })

      expect(nodeId!).toBeTruthy()
      const node = result.current.tree.nodes.get(nodeId!)
      expect(node).toBeDefined()
      expect(node!.san).toBe('c5')
      expect(node!.fen).toBe('fen-after-c5')
      expect(node!.parentId).toBeNull()
      expect(node!.parentGameIndex).toBe(4)
      expect(node!.branchPlyOffset).toBe(0)
      expect(node!.nestingLevel).toBe(0)
      expect(node!.children).toEqual([])

      const rootBranches = result.current.tree.rootBranches.get(4)
      expect(rootBranches).toEqual([nodeId!])
    })

    it('deduplicates root branches with same SAN at same moveIndex', () => {
      const { result } = renderHook(() => useVariationTree())

      let id1: string, id2: string
      act(() => {
        id1 = result.current.addMove({
          san: 'c5',
          fen: 'fen-after-c5',
          fenBefore: 'fen-before-c5',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })
      act(() => {
        id2 = result.current.addMove({
          san: 'c5',
          fen: 'fen-after-c5',
          fenBefore: 'fen-before-c5',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })

      expect(id1!).toBe(id2!)
      expect(result.current.tree.rootBranches.get(4)!.length).toBe(1)
    })

    it('allows different SANs at same moveIndex', () => {
      const { result } = renderHook(() => useVariationTree())

      let id1: string, id2: string
      act(() => {
        id1 = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })
      act(() => {
        id2 = result.current.addMove({
          san: 'e5',
          fen: 'fen-e5',
          fenBefore: 'fen-before',
          uci: 'e7e5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })

      expect(id1!).not.toBe(id2!)
      expect(result.current.tree.rootBranches.get(4)!.length).toBe(2)
    })

    it('adds continuation as first child of variation node', () => {
      const { result } = renderHook(() => useVariationTree())

      let rootId: string, childId: string
      act(() => {
        rootId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })
      act(() => {
        childId = result.current.addMove({
          san: 'Nf3',
          fen: 'fen-nf3',
          fenBefore: 'fen-c5',
          uci: 'g1f3',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })

      const child = result.current.tree.nodes.get(childId!)!
      expect(child.parentId).toBe(rootId!)
      expect(child.branchPlyOffset).toBe(1)
      expect(child.nestingLevel).toBe(0) // continuation, not a sub-branch

      const root = result.current.tree.nodes.get(rootId!)!
      expect(root.children).toEqual([childId!])
    })

    it('adds sub-branch as additional child with incremented nesting', () => {
      const { result } = renderHook(() => useVariationTree())

      let rootId: string, contId: string, branchId: string
      act(() => {
        rootId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })
      act(() => {
        contId = result.current.addMove({
          san: 'Nf3',
          fen: 'fen-nf3',
          fenBefore: 'fen-c5',
          uci: 'g1f3',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })
      act(() => {
        branchId = result.current.addMove({
          san: 'd4',
          fen: 'fen-d4',
          fenBefore: 'fen-c5',
          uci: 'd2d4',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })

      const branch = result.current.tree.nodes.get(branchId!)!
      expect(branch.parentId).toBe(rootId!)
      expect(branch.branchPlyOffset).toBe(0) // new sub-branch starts at 0
      expect(branch.nestingLevel).toBe(1) // one level deeper

      const root = result.current.tree.nodes.get(rootId!)!
      expect(root.children).toEqual([contId!, branchId!])
    })

    it('deduplicates children of variation nodes', () => {
      const { result } = renderHook(() => useVariationTree())

      let rootId: string
      act(() => {
        rootId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })

      let id1: string, id2: string
      act(() => {
        id1 = result.current.addMove({
          san: 'Nf3',
          fen: 'fen-nf3',
          fenBefore: 'fen-c5',
          uci: 'g1f3',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })
      act(() => {
        id2 = result.current.addMove({
          san: 'Nf3',
          fen: 'fen-nf3',
          fenBefore: 'fen-c5',
          uci: 'g1f3',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })

      expect(id1!).toBe(id2!)
      const root = result.current.tree.nodes.get(rootId!)!
      expect(root.children.length).toBe(1)
    })

    it('handles branch from starting position (moveIndex -1)', () => {
      const { result } = renderHook(() => useVariationTree())

      let nodeId: string
      act(() => {
        nodeId = result.current.addMove({
          san: 'd4',
          fen: 'fen-d4',
          fenBefore: 'startpos',
          uci: 'd2d4',
          parentContext: { type: 'game', moveIndex: -1 },
        })
      })

      expect(result.current.tree.rootBranches.get(-1)).toEqual([nodeId!])
      const node = result.current.tree.nodes.get(nodeId!)!
      expect(node.parentGameIndex).toBe(-1)
    })
  })

  describe('navigateUp', () => {
    it('returns game move for root branch node', () => {
      const { result } = renderHook(() => useVariationTree())

      let nodeId: string
      act(() => {
        nodeId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })

      const up = result.current.navigateUp(nodeId!)
      expect(up).toEqual({ type: 'game', moveIndex: 4 })
    })

    it('returns parent variation node for nested node', () => {
      const { result } = renderHook(() => useVariationTree())

      let rootId: string, childId: string
      act(() => {
        rootId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })
      act(() => {
        childId = result.current.addMove({
          san: 'Nf3',
          fen: 'fen-nf3',
          fenBefore: 'fen-c5',
          uci: 'g1f3',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })

      const up = result.current.navigateUp(childId!)
      expect(up).toEqual({ type: 'variation', nodeId: rootId! })
    })

    it('returns null for unknown nodeId', () => {
      const { result } = renderHook(() => useVariationTree())
      const up = result.current.navigateUp('nonexistent')
      expect(up).toBeNull()
    })
  })

  describe('navigateDown', () => {
    it('returns first child nodeId', () => {
      const { result } = renderHook(() => useVariationTree())

      let rootId: string, childId: string
      act(() => {
        rootId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })
      act(() => {
        childId = result.current.addMove({
          san: 'Nf3',
          fen: 'fen-nf3',
          fenBefore: 'fen-c5',
          uci: 'g1f3',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })

      expect(result.current.navigateDown(rootId!)).toBe(childId!)
    })

    it('returns null at leaf node', () => {
      const { result } = renderHook(() => useVariationTree())

      let nodeId: string
      act(() => {
        nodeId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })

      expect(result.current.navigateDown(nodeId!)).toBeNull()
    })
  })

  describe('getAbsolutePly', () => {
    it('returns parentGameIndex + 1 for root node', () => {
      const { result } = renderHook(() => useVariationTree())

      let nodeId: string
      act(() => {
        nodeId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })

      // parentGameIndex=4, depth=0 → absolute ply = 5
      expect(result.current.getAbsolutePly(nodeId!)).toBe(5)
    })

    it('increments ply for each level of depth', () => {
      const { result } = renderHook(() => useVariationTree())

      let rootId: string, child1Id: string, child2Id: string
      act(() => {
        rootId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })
      act(() => {
        child1Id = result.current.addMove({
          san: 'Nf3',
          fen: 'fen-nf3',
          fenBefore: 'fen-c5',
          uci: 'g1f3',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })
      act(() => {
        child2Id = result.current.addMove({
          san: 'd6',
          fen: 'fen-d6',
          fenBefore: 'fen-nf3',
          uci: 'd7d6',
          parentContext: { type: 'variation', nodeId: child1Id! },
        })
      })

      // parentGameIndex=4, root depth=0 → 5
      // child1 depth=1 → 6
      // child2 depth=2 → 7
      expect(result.current.getAbsolutePly(rootId!)).toBe(5)
      expect(result.current.getAbsolutePly(child1Id!)).toBe(6)
      expect(result.current.getAbsolutePly(child2Id!)).toBe(7)
    })

    it('handles starting position branches (moveIndex -1)', () => {
      const { result } = renderHook(() => useVariationTree())

      let nodeId: string
      act(() => {
        nodeId = result.current.addMove({
          san: 'd4',
          fen: 'fen-d4',
          fenBefore: 'startpos',
          uci: 'd2d4',
          parentContext: { type: 'game', moveIndex: -1 },
        })
      })

      // parentGameIndex=-1, depth=0 → absolute ply = 0
      expect(result.current.getAbsolutePly(nodeId!)).toBe(0)
    })
  })

  describe('collectBranchNodes', () => {
    it('collects nodes following children[0] chain', () => {
      const { result } = renderHook(() => useVariationTree())

      let rootId: string, child1Id: string, child2Id: string
      act(() => {
        rootId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })
      act(() => {
        child1Id = result.current.addMove({
          san: 'Nf3',
          fen: 'fen-nf3',
          fenBefore: 'fen-c5',
          uci: 'g1f3',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })
      act(() => {
        child2Id = result.current.addMove({
          san: 'd6',
          fen: 'fen-d6',
          fenBefore: 'fen-nf3',
          uci: 'd7d6',
          parentContext: { type: 'variation', nodeId: child1Id! },
        })
      })

      const branch = result.current.collectBranchNodes(rootId!)
      expect(branch.map(n => n.id)).toEqual([rootId!, child1Id!, child2Id!])
    })

    it('does not follow sub-branches (children[1+])', () => {
      const { result } = renderHook(() => useVariationTree())

      let rootId: string, contId: string
      act(() => {
        rootId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })
      act(() => {
        contId = result.current.addMove({
          san: 'Nf3',
          fen: 'fen-nf3',
          fenBefore: 'fen-c5',
          uci: 'g1f3',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })
      // Add sub-branch (children[1])
      act(() => {
        result.current.addMove({
          san: 'd4',
          fen: 'fen-d4',
          fenBefore: 'fen-c5',
          uci: 'd2d4',
          parentContext: { type: 'variation', nodeId: rootId! },
        })
      })

      const branch = result.current.collectBranchNodes(rootId!)
      expect(branch.map(n => n.id)).toEqual([rootId!, contId!])
    })
  })

  describe('analysis cache with request-keyed routing', () => {
    it('registers and resolves pending requests', () => {
      const { result } = renderHook(() => useVariationTree())

      act(() => {
        result.current.registerPending('req-1', 'fen-position-1')
      })

      const analysis = makeResult('req-1', 'fen-position-1')
      act(() => {
        result.current.resolvePending('req-1', analysis)
      })

      expect(result.current.getVarAnalysis('fen-position-1')).toEqual(analysis)
    })

    it('resolves concurrent requests out of order correctly', () => {
      const { result } = renderHook(() => useVariationTree())

      // Register two requests
      act(() => {
        result.current.registerPending('req-1', 'fen-A')
        result.current.registerPending('req-2', 'fen-B')
      })

      const analysisB = makeResult('req-2', 'fen-B')
      const analysisA = makeResult('req-1', 'fen-A')

      // Resolve out of order: req-2 first, then req-1
      act(() => {
        result.current.resolvePending('req-2', analysisB)
      })
      act(() => {
        result.current.resolvePending('req-1', analysisA)
      })

      // Each should be cached under the correct FEN
      expect(result.current.getVarAnalysis('fen-A')).toEqual(analysisA)
      expect(result.current.getVarAnalysis('fen-B')).toEqual(analysisB)
    })

    it('ignores resolution of unknown request IDs', () => {
      const { result } = renderHook(() => useVariationTree())

      const analysis = makeResult('unknown', 'some-fen')
      act(() => {
        result.current.resolvePending('unknown', analysis)
      })

      expect(result.current.getVarAnalysis('some-fen')).toBeUndefined()
    })

    it('clears cache on clearTree', () => {
      const { result } = renderHook(() => useVariationTree())

      act(() => {
        result.current.registerPending('req-1', 'fen-A')
        result.current.resolvePending('req-1', makeResult('req-1', 'fen-A'))
      })

      expect(result.current.getVarAnalysis('fen-A')).toBeDefined()

      act(() => {
        result.current.clearTree()
      })

      expect(result.current.getVarAnalysis('fen-A')).toBeUndefined()
      expect(result.current.tree.nodes.size).toBe(0)
      expect(result.current.tree.rootBranches.size).toBe(0)
      expect(result.current.selectedVarNodeId).toBeNull()
    })
  })

  describe('selectedVarNodeId', () => {
    it('starts null and can be set', () => {
      const { result } = renderHook(() => useVariationTree())
      expect(result.current.selectedVarNodeId).toBeNull()

      let nodeId: string
      act(() => {
        nodeId = result.current.addMove({
          san: 'c5',
          fen: 'fen-c5',
          fenBefore: 'fen-before',
          uci: 'c7c5',
          parentContext: { type: 'game', moveIndex: 4 },
        })
      })

      act(() => {
        result.current.setSelectedVarNode(nodeId!)
      })

      expect(result.current.selectedVarNodeId).toBe(nodeId!)
    })
  })
})
