import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render } from '../test/utils'
import VariationLine from './VariationLine'
import type { VariationTree, VarNode, VariationNodeId } from '../types/variationTree'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let nextId = 0
function makeId(): VariationNodeId {
  return `node-${nextId++}`
}

function makeNode(overrides: Partial<VarNode> & { id: VariationNodeId; san: string }): VarNode {
  return {
    fen: 'fen',
    fenBefore: 'fenBefore',
    uci: 'e2e4',
    parentId: null,
    parentGameIndex: 0,
    branchPlyOffset: 0,
    children: [],
    nestingLevel: 0,
    ...overrides,
  }
}

function makeTree(nodes: VarNode[]): VariationTree {
  const tree: VariationTree = {
    nodes: new Map(),
    rootBranches: new Map(),
  }
  for (const n of nodes) {
    tree.nodes.set(n.id, n)
  }
  return tree
}

/**
 * Create a simple chain of nodes (children[0] continuation).
 * Returns the array of nodes and a tree.
 */
function makeChain(
  sans: string[],
  opts: { parentGameIndex?: number; nestingLevel?: number } = {},
): { nodes: VarNode[]; tree: VariationTree } {
  const { parentGameIndex = 4, nestingLevel = 0 } = opts
  const chainNodes: VarNode[] = []
  for (let i = 0; i < sans.length; i++) {
    const id = makeId()
    chainNodes.push(
      makeNode({
        id,
        san: sans[i],
        parentId: i === 0 ? null : chainNodes[i - 1].id,
        parentGameIndex,
        branchPlyOffset: i,
        nestingLevel,
        children: [],
      }),
    )
  }
  // Wire up children[0] links
  for (let i = 0; i < chainNodes.length - 1; i++) {
    chainNodes[i] = { ...chainNodes[i], children: [chainNodes[i + 1].id] }
  }
  return { nodes: chainNodes, tree: makeTree(chainNodes) }
}

/**
 * getAbsolutePly mock: parentGameIndex + 1 + depth from root.
 */
function makeGetAbsolutePly(tree: VariationTree) {
  return (nodeId: VariationNodeId): number => {
    const node = tree.nodes.get(nodeId)
    if (!node) return 0
    let depth = 0
    let current: VarNode | undefined = node
    while (current && current.parentId) {
      depth++
      current = tree.nodes.get(current.parentId)
    }
    const gameIndex = current?.parentGameIndex ?? node.parentGameIndex
    return gameIndex + 1 + depth
  }
}

beforeEach(() => {
  nextId = 0
})

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('VariationLine', () => {
  it('renders move numbers: white ply gets "N." format, black ply gets "N..." format', () => {
    // parentGameIndex=4 (even, after white move) → first ply is at absPly=5 (black)
    const { nodes, tree } = makeChain(['c5', 'Nf3'], { parentGameIndex: 4 })
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )
    const moveNumbers = container.querySelectorAll('.variation-move-number')
    // absPly=5 → fullmove=3, isBlack=true → "3..."
    expect(moveNumbers[0].textContent).toBe('3...')
    // absPly=6 → fullmove=4, isBlack=false → "4. "
    expect(moveNumbers[1].textContent).toBe('4. ')
  })

  it('first ply always has a move number, even if it is a continuation ply', () => {
    // parentGameIndex=3 (odd, after black move) → first ply is at absPly=4 (white)
    const { nodes, tree } = makeChain(['Nf3', 'e5'], { parentGameIndex: 3 })
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )
    const moveNumbers = container.querySelectorAll('.variation-move-number')
    expect(moveNumbers.length).toBeGreaterThanOrEqual(1)
    // absPly=4 → fullmove=3, isBlack=false → "3. "
    expect(moveNumbers[0].textContent).toBe('3. ')
  })

  it('showPrefix=false renders no "|- " prefix', () => {
    const { nodes, tree } = makeChain(['Nf3'])
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )
    expect(container.querySelector('.variation-prefix')).toBeNull()
  })

  it('showPrefix=true renders "|- " prefix at depth 0', () => {
    const { nodes, tree } = makeChain(['Nf3'])
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={true}
      />,
    )
    const prefix = container.querySelector('.variation-prefix')
    expect(prefix).not.toBeNull()
    expect(prefix!.textContent).toBe('|- ')
  })

  it('showPrefix=true at depth=1 renders "|  |- " prefix', () => {
    const { nodes, tree } = makeChain(['Nf3'])
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={true}
        depth={1}
      />,
    )
    const prefix = container.querySelector('.variation-prefix')
    expect(prefix).not.toBeNull()
    expect(prefix!.textContent).toBe('|  |- ')
  })

  it('showPrefix=true at depth=2 renders "|  |  |- " prefix', () => {
    const { nodes, tree } = makeChain(['Nf3'])
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={true}
        depth={2}
      />,
    )
    const prefix = container.querySelector('.variation-prefix')
    expect(prefix).not.toBeNull()
    expect(prefix!.textContent).toBe('|  |  |- ')
  })

  it('fires onNodeClick with correct nodeId on ply click', () => {
    const { nodes, tree } = makeChain(['Nf3', 'e5'])
    const onClick = vi.fn()
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={onClick}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )
    const plies = container.querySelectorAll('.variation-ply')
    plies[1].dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(onClick).toHaveBeenCalledWith(nodes[1].id)
  })

  it('applies variation-ply--selected to the correct ply', () => {
    const { nodes, tree } = makeChain(['Nf3', 'e5', 'Bc4'])
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={nodes[1].id}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )
    const plies = container.querySelectorAll('.variation-ply')
    expect(plies[0].classList.contains('variation-ply--selected')).toBe(false)
    expect(plies[1].classList.contains('variation-ply--selected')).toBe(true)
    expect(plies[2].classList.contains('variation-ply--selected')).toBe(false)
  })

  it('selected ply includes move number in the highlight span', () => {
    const { nodes, tree } = makeChain(['Nf3'], { parentGameIndex: 3 })
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={nodes[0].id}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )
    const selectedPly = container.querySelector('.variation-ply--selected')
    expect(selectedPly).not.toBeNull()
    // Move number span is inside the selected ply span
    const moveNum = selectedPly!.querySelector('.variation-move-number')
    expect(moveNum).not.toBeNull()
  })

  it('renders continuation (children[0]) first, then sub-branches at a branch point', () => {
    // Build a tree: root → A, where A has children [B (continuation), C (sub-branch)]
    const rootId = makeId()
    const aId = makeId()
    const bId = makeId()
    const cId = makeId()

    const root = makeNode({ id: rootId, san: 'c5', parentGameIndex: 4, children: [aId] })
    const a = makeNode({ id: aId, san: 'Nf3', parentId: rootId, parentGameIndex: 4, branchPlyOffset: 1, children: [bId, cId] })
    const b = makeNode({ id: bId, san: 'e5', parentId: aId, parentGameIndex: 4, branchPlyOffset: 2, nestingLevel: 0 })
    const c = makeNode({ id: cId, san: 'd5', parentId: aId, parentGameIndex: 4, branchPlyOffset: 0, nestingLevel: 1 })

    const tree = makeTree([root, a, b, c])

    const { container } = render(
      <VariationLine
        rootNodeId={rootId}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )

    // Should have 3 variation-line divs: main inline, continuation (B), sub-branch (C)
    const lines = container.querySelectorAll('.variation-line')
    expect(lines.length).toBe(3)

    // First line: root + A inline (c5 Nf3)
    const firstLinePlies = lines[0].querySelectorAll(':scope > .variation-ply')
    expect(firstLinePlies.length).toBe(2)
    expect(firstLinePlies[0].textContent).toContain('c5')
    expect(firstLinePlies[1].textContent).toContain('Nf3')

    // Second line: continuation B (e5) — children[0] rendered first
    const secondLinePlies = lines[1].querySelectorAll(':scope > .variation-ply')
    expect(secondLinePlies[0].textContent).toContain('e5')

    // Third line: sub-branch C (d5) — children[1] rendered second
    const thirdLinePlies = lines[2].querySelectorAll(':scope > .variation-ply')
    expect(thirdLinePlies[0].textContent).toContain('d5')
  })

  it('renders sibling branches in children array order (chronological)', () => {
    // A has children: [B, C, D] — three branches
    const aId = makeId()
    const bId = makeId()
    const cId = makeId()
    const dId = makeId()

    const a = makeNode({ id: aId, san: 'c5', parentGameIndex: 4, children: [bId, cId, dId] })
    const b = makeNode({ id: bId, san: 'Nf3', parentId: aId, parentGameIndex: 4, branchPlyOffset: 1, nestingLevel: 0 })
    const c = makeNode({ id: cId, san: 'Nc3', parentId: aId, parentGameIndex: 4, branchPlyOffset: 0, nestingLevel: 1 })
    const d = makeNode({ id: dId, san: 'e3', parentId: aId, parentGameIndex: 4, branchPlyOffset: 0, nestingLevel: 1 })

    const tree = makeTree([a, b, c, d])

    const { container } = render(
      <VariationLine
        rootNodeId={aId}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )

    const lines = container.querySelectorAll('.variation-line')
    // 1: main (c5), 2: continuation B (Nf3), 3: branch C (Nc3), 4: branch D (e3)
    expect(lines.length).toBe(4)

    const getFirstSan = (line: Element) =>
      line.querySelector(':scope > .variation-ply')?.textContent ?? ''

    expect(getFirstSan(lines[1])).toContain('Nf3')
    expect(getFirstSan(lines[2])).toContain('Nc3')
    expect(getFirstSan(lines[3])).toContain('e3')
  })

  it('children[0] split from branch point gets "|- " prefix', () => {
    const aId = makeId()
    const bId = makeId()
    const cId = makeId()

    const a = makeNode({ id: aId, san: 'c5', parentGameIndex: 4, children: [bId, cId] })
    const b = makeNode({ id: bId, san: 'Nf3', parentId: aId, parentGameIndex: 4, branchPlyOffset: 1, nestingLevel: 0 })
    const c = makeNode({ id: cId, san: 'Nc3', parentId: aId, parentGameIndex: 4, branchPlyOffset: 0, nestingLevel: 1 })

    const tree = makeTree([a, b, c])

    const { container } = render(
      <VariationLine
        rootNodeId={aId}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )

    const lines = container.querySelectorAll('.variation-line')
    // First line (main inline): no prefix (showPrefix=false from parent)
    expect(lines[0].querySelector('.variation-prefix')).toBeNull()
    // Second line (continuation B): has prefix (showPrefix=true from recursion)
    expect(lines[1].querySelector('.variation-prefix')).not.toBeNull()
    // Third line (sub-branch C): has prefix
    expect(lines[2].querySelector('.variation-prefix')).not.toBeNull()
  })

  it('all siblings at a branch point render with the same prefix depth', () => {
    // Root line (no prefix) branches into 3 children — all should get "|- " (depth 0)
    const aId = makeId()
    const bId = makeId()
    const cId = makeId()
    const dId = makeId()

    const a = makeNode({ id: aId, san: 'c5', parentGameIndex: 4, children: [bId, cId, dId] })
    const b = makeNode({ id: bId, san: 'Nf3', parentId: aId, parentGameIndex: 4, branchPlyOffset: 1, nestingLevel: 0 })
    const c = makeNode({ id: cId, san: 'Nc3', parentId: aId, parentGameIndex: 4, branchPlyOffset: 0, nestingLevel: 1 })
    const d = makeNode({ id: dId, san: 'e3', parentId: aId, parentGameIndex: 4, branchPlyOffset: 0, nestingLevel: 1 })

    const tree = makeTree([a, b, c, d])

    const { container } = render(
      <VariationLine
        rootNodeId={aId}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )

    const prefixes = container.querySelectorAll('.variation-prefix')
    // All 3 child lines should have the same prefix text: "|- "
    expect(prefixes.length).toBe(3)
    for (const prefix of prefixes) {
      expect(prefix.textContent).toBe('|- ')
    }
  })

  it('nested branches get deeper prefix than their parent branches', () => {
    // Root (no prefix) → A branches into [B, C]
    // B (depth 0 prefix) → B continues to B2 → B2 branches into [B3, B4]
    // B3 and B4 should get depth 1 prefix: "|  |- "
    const rootId = makeId()
    const bId = makeId()
    const cId = makeId()
    const b2Id = makeId()
    const b3Id = makeId()
    const b4Id = makeId()

    const root = makeNode({ id: rootId, san: 'c5', parentGameIndex: 4, children: [bId, cId] })
    const b = makeNode({ id: bId, san: 'Nf3', parentId: rootId, parentGameIndex: 4, branchPlyOffset: 1, nestingLevel: 0, children: [b2Id] })
    const c = makeNode({ id: cId, san: 'Nc3', parentId: rootId, parentGameIndex: 4, branchPlyOffset: 0, nestingLevel: 1 })
    const b2 = makeNode({ id: b2Id, san: 'e5', parentId: bId, parentGameIndex: 4, branchPlyOffset: 2, nestingLevel: 0, children: [b3Id, b4Id] })
    const b3 = makeNode({ id: b3Id, san: 'Bc4', parentId: b2Id, parentGameIndex: 4, branchPlyOffset: 3, nestingLevel: 0 })
    const b4 = makeNode({ id: b4Id, san: 'd4', parentId: b2Id, parentGameIndex: 4, branchPlyOffset: 0, nestingLevel: 1 })

    const tree = makeTree([root, b, c, b2, b3, b4])

    const { container } = render(
      <VariationLine
        rootNodeId={rootId}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )

    const prefixes = container.querySelectorAll('.variation-prefix')
    // B and C get "|- " (depth 0), B3 and B4 get "|  |- " (depth 1)
    expect(prefixes.length).toBe(4)
    expect(prefixes[0].textContent).toBe('|- ')  // B
    expect(prefixes[1].textContent).toBe('|  |- ')  // B3
    expect(prefixes[2].textContent).toBe('|  |- ')  // B4
    expect(prefixes[3].textContent).toBe('|- ')  // C
  })

  it('subsequent black plies do not get move numbers', () => {
    // parentGameIndex=3 → first ply is absPly=4 (white), second is absPly=5 (black),
    // third is absPly=6 (white)
    const { nodes, tree } = makeChain(['Nf3', 'e5', 'Bc4'], { parentGameIndex: 3 })
    const { container } = render(
      <VariationLine
        rootNodeId={nodes[0].id}
        tree={tree}
        selectedNodeId={null}
        onNodeClick={() => {}}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        showPrefix={false}
      />,
    )
    const plies = container.querySelectorAll('.variation-ply')
    // Ply 0 (Nf3, white): has move number
    expect(plies[0].querySelector('.variation-move-number')).not.toBeNull()
    // Ply 1 (e5, black, not first): no move number
    expect(plies[1].querySelector('.variation-move-number')).toBeNull()
    // Ply 2 (Bc4, white): has move number
    expect(plies[2].querySelector('.variation-move-number')).not.toBeNull()
  })
})
