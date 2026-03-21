import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render } from '../test/utils'
import MoveList from './MoveList'
import type { VariationTree, VarNode, VariationNodeId } from '../types/variationTree'
import type { NavigateUpResult } from '../hooks/useVariationTree'

// jsdom doesn't implement scrollIntoView
Element.prototype.scrollIntoView = vi.fn()

const noop = () => {}

/**
 * Render MoveList and return the delta text for each move cell.
 */
function renderAndGetDeltas(
  moves: Array<{
    san: string
    eval?: number | null
    classification?: null
  }>,
  currentIndex: number | null = null,
) {
  const { container } = render(
    <MoveList moves={moves} currentIndex={currentIndex} onNavigate={noop} />,
  )
  const evalSpans = container.querySelectorAll('.move-eval')
  return Array.from(evalSpans).map((el) => el.textContent ?? '')
}

function renderAndGetHeaderEval(
  moves: Array<{
    san: string
    eval?: number | null
    classification?: null
  }>,
  currentIndex: number | null = null,
) {
  const { container } = render(
    <MoveList moves={moves} currentIndex={currentIndex} onNavigate={noop} />,
  )
  const header = container.querySelector('.move-list-header-eval')
  return header?.textContent ?? ''
}

describe('MoveList eval formulas', () => {
  it('shows the full eval formula when white improves after a white move', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 30 },
    ])
    expect(deltas[0]).toBe('0 +0.3 = +0.3')
  })

  it('shows the full eval formula when white improves after a bad black move', () => {
    const deltas = renderAndGetDeltas([
      { san: 'd4', eval: 30 },
      { san: 'h5', eval: 160 },
    ])
    expect(deltas[1]).toBe('+0.3 +1.3 = +1.6')
  })

  it('shows the full eval formula when eval drops for white', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 100 },
      { san: 'e5', eval: 50 },
      { san: 'Qh5', eval: -100 },
    ])
    expect(deltas[2]).toBe('+0.5 −1.5 = −1')
  })

  it('shows the full eval formula when eval does not change', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 30 },
      { san: 'e5', eval: 30 },
    ])
    expect(deltas[1]).toBe('+0.3 +0 = +0.3')
  })

  it('rounds values less than 5cp to 0 in the displayed formula', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 0 },
      { san: 'e5', eval: 4 },
    ])
    expect(deltas[1]).toBe('0 +0.0 = +0.0')
  })

  it('shows the rounded delta for 5cp or more', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 0 },
      { san: 'e5', eval: 50 },
    ])
    expect(deltas[1]).toBe('0 +0.5 = +0.5')
  })

  it('shows nothing when eval is not available', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4' },
    ])
    expect(deltas[0]).toBe('')
  })

  it('shows nothing when previous eval is not available', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4' },
      { san: 'e5', eval: 50 },
    ])
    expect(deltas[1]).toBe('')
  })

  it('first move uses 0 as baseline (starting position)', () => {
    const deltas = renderAndGetDeltas([
      { san: 'e4', eval: 50 },
    ])
    expect(deltas[0]).toBe('0 +0.5 = +0.5')
  })
})

describe('MoveList header eval', () => {
  it('shows eval of selected move in the header', () => {
    const headerEval = renderAndGetHeaderEval(
      [
        { san: 'e4', eval: 30 },
        { san: 'e5', eval: 50 },
      ],
      0,
    )
    expect(headerEval).toBe('+0.3')
  })

  it('shows eval of last move when currentIndex is null', () => {
    const headerEval = renderAndGetHeaderEval(
      [
        { san: 'e4', eval: 30 },
        { san: 'e5', eval: 50 },
      ],
      null,
    )
    expect(headerEval).toBe('+0.5')
  })

  it('shows nothing when no eval is available', () => {
    const headerEval = renderAndGetHeaderEval(
      [{ san: 'e4' }],
      0,
    )
    expect(headerEval).toBe('')
  })
})

// ---------------------------------------------------------------------------
// Variation integration helpers
// ---------------------------------------------------------------------------

const GAME_MOVES = [
  { san: 'e4', eval: 30 },
  { san: 'e5', eval: 20 },
  { san: 'Nf3', eval: 40 },
  { san: 'Nc6', eval: 30 },
  { san: 'Bb5', eval: 50 },
  { san: 'a6', eval: 40 },
]

function makeNode(overrides: Partial<VarNode> & { id: string; san: string }): VarNode {
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

function makeTree(nodes: VarNode[], rootBranches: Map<number, VariationNodeId[]>): VariationTree {
  const nodeMap = new Map<VariationNodeId, VarNode>()
  for (const n of nodes) nodeMap.set(n.id, n)
  return { nodes: nodeMap, rootBranches }
}

function makeGetAbsolutePly(tree: VariationTree) {
  return (nodeId: VariationNodeId): number => {
    const node = tree.nodes.get(nodeId)
    if (!node) return 0
    let depth = 0
    let current: VarNode | undefined = node
    while (current?.parentId) {
      depth++
      current = tree.nodes.get(current.parentId)
    }
    return (current?.parentGameIndex ?? 0) + 1 + depth
  }
}

// Full variation prop set for isVariationActive
function variationActiveProps(
  tree: VariationTree,
  selectedId: VariationNodeId,
  overrides?: {
    onVarSelect?: (id: VariationNodeId | null) => void
    navigateUp?: (id: VariationNodeId) => NavigateUpResult | null
    navigateDown?: (id: VariationNodeId) => VariationNodeId | null
  },
) {
  return {
    variationTree: tree,
    selectedVarNodeId: selectedId,
    onVarSelect: overrides?.onVarSelect ?? vi.fn(),
    getAbsolutePly: makeGetAbsolutePly(tree),
    navigateUp: overrides?.navigateUp ?? vi.fn(() => null),
    navigateDown: overrides?.navigateDown ?? vi.fn(() => null),
  }
}

// ---------------------------------------------------------------------------
// Variation integration tests
// ---------------------------------------------------------------------------

describe('MoveList variation integration', () => {
  describe('branch anchoring parity', () => {
    it('branch at whiteIdx (even) renders after the pair, first ply is black', () => {
      // Branch at whiteIdx=4 (after white move Bb5) → variation starts with black ply
      const varNode = makeNode({
        id: 'v1',
        san: 'Nf6', // black response alternative
        parentGameIndex: 4,
      })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={0}
          onNavigate={noop}
          variationTree={tree}
          getAbsolutePly={makeGetAbsolutePly(tree)}
          onVarSelect={noop}
        />,
      )

      const variationLines = container.querySelectorAll('.variation-line')
      expect(variationLines.length).toBe(1)

      // Variation ply should contain a black move number (absPly=5, fullmove=3, isBlack)
      const moveNum = variationLines[0].querySelector('.variation-move-number')
      expect(moveNum?.textContent).toBe('3...')
    })

    it('branch at blackIdx (odd) renders after the pair, first ply is white', () => {
      // Branch at blackIdx=5 (after black move a6) → variation starts with white ply
      const varNode = makeNode({
        id: 'v1',
        san: 'Ba4', // white alternative
        parentGameIndex: 5,
      })
      const tree = makeTree([varNode], new Map([[5, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={0}
          onNavigate={noop}
          variationTree={tree}
          getAbsolutePly={makeGetAbsolutePly(tree)}
          onVarSelect={noop}
        />,
      )

      const variationLines = container.querySelectorAll('.variation-line')
      expect(variationLines.length).toBe(1)

      // absPly=6 → fullmove=4, isBlack=false → white format "4. "
      const moveNum = variationLines[0].querySelector('.variation-move-number')
      expect(moveNum?.textContent).toBe('4. ')
    })
  })

  it('starting-position branches render before the first pair', () => {
    const varNode = makeNode({
      id: 'v-start',
      san: 'd4',
      parentGameIndex: -1,
    })
    const tree = makeTree([varNode], new Map([[-1, ['v-start']]]))

    const { container } = render(
      <MoveList
        moves={GAME_MOVES}
        currentIndex={0}
        onNavigate={noop}
        variationTree={tree}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        onVarSelect={noop}
      />,
    )

    const grid = container.querySelector('.move-list-grid')!
    const children = Array.from(grid.children)
    // First 3 children are headers, then variation line should come before any MoveRow
    // Find first variation-line and first move-number
    const firstVarLine = children.findIndex(el => el.classList.contains('variation-line'))
    const firstMoveNumber = children.findIndex(el => el.classList.contains('move-number'))
    expect(firstVarLine).toBeLessThan(firstMoveNumber)
  })

  it('multiple branches from same point render in rootBranches array order', () => {
    const v1 = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
    const v2 = makeNode({ id: 'v2', san: 'Be7', parentGameIndex: 4 })
    const tree = makeTree([v1, v2], new Map([[4, ['v1', 'v2']]]))

    const { container } = render(
      <MoveList
        moves={GAME_MOVES}
        currentIndex={0}
        onNavigate={noop}
        variationTree={tree}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        onVarSelect={noop}
      />,
    )

    const variationLines = container.querySelectorAll('.variation-line')
    expect(variationLines.length).toBe(2)
    expect(variationLines[0].textContent).toContain('Nf6')
    expect(variationLines[1].textContent).toContain('Be7')
  })

  it('bubble messages and variation branches coexist', () => {
    const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
    const tree = makeTree([varNode], new Map([[4, ['v1']]]))

    const messages = new Map([
      [4, [{ key: 'msg1', variant: 'srs-pass' as const, text: 'Nice!' }]],
    ])

    const { container } = render(
      <MoveList
        moves={GAME_MOVES}
        currentIndex={4}
        onNavigate={noop}
        variationTree={tree}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        onVarSelect={noop}
        messages={messages}
      />,
    )

    // Both bubble and variation should render
    const bubbles = container.querySelectorAll('.move-bubble')
    const variationLines = container.querySelectorAll('.variation-line')
    expect(bubbles.length).toBeGreaterThan(0)
    expect(variationLines.length).toBe(1)
  })

  describe('selection suppression', () => {
    it('no MoveRow has .selected when isVariationActive', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={2}
          onNavigate={noop}
          {...variationActiveProps(tree, 'v1')}
        />,
      )

      const selectedButtons = container.querySelectorAll('.move-button.selected')
      expect(selectedButtons.length).toBe(0)
    })

    it('MoveRows keep .selected when selectedVarNodeId set but callbacks missing', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={2}
          onNavigate={noop}
          variationTree={tree}
          selectedVarNodeId="v1"
          getAbsolutePly={makeGetAbsolutePly(tree)}
          // Missing onVarSelect, navigateUp, navigateDown → not isVariationActive
        />,
      )

      const selectedButtons = container.querySelectorAll('.move-button.selected')
      expect(selectedButtons.length).toBeGreaterThan(0)
    })
  })

  describe('header eval override', () => {
    it('shows headerEvalOverride when isVariationActive', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={2}
          onNavigate={noop}
          {...variationActiveProps(tree, 'v1')}
          headerEvalOverride="+2.5"
        />,
      )

      const header = container.querySelector('.move-list-header-eval')
      expect(header?.textContent).toBe('+2.5')
    })

    it('shows empty header when isVariationActive and no override provided', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={2}
          onNavigate={noop}
          {...variationActiveProps(tree, 'v1')}
        />,
      )

      const header = container.querySelector('.move-list-header-eval')
      expect(header?.textContent).toBe('')
    })

    it('shows main-line eval when selectedVarNodeId set but not isVariationActive', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={2}
          onNavigate={noop}
          variationTree={tree}
          selectedVarNodeId="v1"
          getAbsolutePly={makeGetAbsolutePly(tree)}
          headerEvalOverride="+2.5"
        />,
      )

      // Should show main-line eval for Nf3 (index 2, eval=40) not the override
      const header = container.querySelector('.move-list-header-eval')
      expect(header?.textContent).toBe('+0.4')
    })
  })

  describe('button navigation', () => {
    it('Prev button calls navigateUp when isVariationActive', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))
      const onVarSelect = vi.fn()
      const onNavigate = vi.fn()
      const navigateUp = vi.fn(() => ({ type: 'game' as const, moveIndex: 4 }))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={4}
          onNavigate={onNavigate}
          {...variationActiveProps(tree, 'v1', { onVarSelect, navigateUp })}
        />,
      )

      const prevBtn = container.querySelector('[title="Previous move (←)"]') as HTMLButtonElement
      prevBtn.click()

      expect(navigateUp).toHaveBeenCalledWith('v1')
      expect(onVarSelect).toHaveBeenCalledWith(null)
      expect(onNavigate).toHaveBeenCalledWith(4)
    })

    it('Next button calls navigateDown when isVariationActive', () => {
      const v1 = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4, children: ['v2'] })
      const v2 = makeNode({ id: 'v2', san: 'Bc4', parentId: 'v1', parentGameIndex: 4, branchPlyOffset: 1 })
      const tree = makeTree([v1, v2], new Map([[4, ['v1']]]))
      const onVarSelect = vi.fn()
      const navigateDown = vi.fn(() => 'v2')

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={4}
          onNavigate={noop}
          {...variationActiveProps(tree, 'v1', { onVarSelect, navigateDown })}
        />,
      )

      const nextBtn = container.querySelector('[title="Next move (→)"]') as HTMLButtonElement
      nextBtn.click()

      expect(navigateDown).toHaveBeenCalledWith('v1')
      expect(onVarSelect).toHaveBeenCalledWith('v2')
    })

    it('Next button is disabled at variation dead end', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={4}
          onNavigate={noop}
          {...variationActiveProps(tree, 'v1')}
        />,
      )

      const nextBtn = container.querySelector('[title="Next move (→)"]') as HTMLButtonElement
      expect(nextBtn.disabled).toBe(true)
    })

    it('buttons use main-line logic when selectedVarNodeId set but callbacks missing', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={2}
          onNavigate={noop}
          variationTree={tree}
          selectedVarNodeId="v1"
          getAbsolutePly={makeGetAbsolutePly(tree)}
          // Missing callbacks → not isVariationActive
        />,
      )

      // Next button should be enabled (main-line: index 2, can go to 3)
      const nextBtn = container.querySelector('[title="Next move (→)"]') as HTMLButtonElement
      expect(nextBtn.disabled).toBe(false)
    })
  })

  describe('keyboard navigation', () => {
    it('ArrowLeft on first variation ply returns to parent game move', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))
      const onVarSelect = vi.fn()
      const onNavigate = vi.fn()
      const navigateUp = vi.fn(() => ({ type: 'game' as const, moveIndex: 4 }))

      render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={4}
          onNavigate={onNavigate}
          {...variationActiveProps(tree, 'v1', { onVarSelect, navigateUp })}
        />,
      )

      fireEvent.keyDown(window, { key: 'ArrowLeft' })

      expect(navigateUp).toHaveBeenCalledWith('v1')
      expect(onVarSelect).toHaveBeenCalledWith(null)
      expect(onNavigate).toHaveBeenCalledWith(4)
    })

    it('ArrowRight at variation dead end does nothing', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))
      const onVarSelect = vi.fn()
      const onNavigate = vi.fn()
      const navigateDown = vi.fn(() => null) // dead end

      render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={4}
          onNavigate={onNavigate}
          {...variationActiveProps(tree, 'v1', { onVarSelect, navigateDown })}
        />,
      )

      fireEvent.keyDown(window, { key: 'ArrowRight' })

      expect(navigateDown).toHaveBeenCalledWith('v1')
      expect(onVarSelect).not.toHaveBeenCalled()
      expect(onNavigate).not.toHaveBeenCalled()
    })

    it('ArrowRight on main-line move does not enter a variation', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 2 })
      const tree = makeTree([varNode], new Map([[2, ['v1']]]))
      const onNavigate = vi.fn()

      render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={2}
          onNavigate={onNavigate}
          variationTree={tree}
          getAbsolutePly={makeGetAbsolutePly(tree)}
          onVarSelect={noop}
          // Not isVariationActive (selectedVarNodeId not set)
        />,
      )

      fireEvent.keyDown(window, { key: 'ArrowRight' })

      // Should advance to next main-line move, not enter variation
      expect(onNavigate).toHaveBeenCalledWith(3)
    })
  })

  it('starting-position variation renders when moves array is empty', () => {
    const varNode = makeNode({
      id: 'v-start',
      san: 'd4',
      parentGameIndex: -1,
    })
    const tree = makeTree([varNode], new Map([[-1, ['v-start']]]))

    const { container } = render(
      <MoveList
        moves={[]}
        currentIndex={null}
        onNavigate={noop}
        variationTree={tree}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        onVarSelect={noop}
      />,
    )

    // Should render the grid with variation, not "No moves yet"
    expect(container.querySelector('.move-list-empty')).toBeNull()
    const variationLines = container.querySelectorAll('.variation-line')
    expect(variationLines.length).toBe(1)
    expect(variationLines[0].textContent).toContain('d4')
  })

  it('no variation ply is highlighted when selectedVarNodeId set but not isVariationActive', () => {
    const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
    const tree = makeTree([varNode], new Map([[4, ['v1']]]))

    const { container } = render(
      <MoveList
        moves={GAME_MOVES}
        currentIndex={2}
        onNavigate={noop}
        variationTree={tree}
        selectedVarNodeId="v1"
        getAbsolutePly={makeGetAbsolutePly(tree)}
        // Missing onVarSelect, navigateUp, navigateDown → not isVariationActive
      />,
    )

    // No variation ply should be highlighted
    expect(container.querySelector('.variation-ply--selected')).toBeNull()
    // Main-line selection should still work
    expect(container.querySelectorAll('.move-button.selected').length).toBeGreaterThan(0)
  })

  describe('whiteIdx branch split layout', () => {
    it('variation line appears between white and black cells for whiteIdx branch', () => {
      // Branch at whiteIdx=4 → split row: white cell, dots, variation, then black cell
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={0}
          onNavigate={noop}
          variationTree={tree}
          getAbsolutePly={makeGetAbsolutePly(tree)}
          onVarSelect={noop}
        />,
      )

      const grid = container.querySelector('.move-list-grid')!
      const children = Array.from(grid.children)

      // Find the variation line, and the move buttons for white (Bb5, idx=4) and black (a6, idx=5)
      const varLineIdx = children.findIndex(el => el.classList.contains('variation-line'))
      const allButtons = children.filter(el => el.classList.contains('move-button'))
      // Bb5 is the 5th move button (0-indexed: e4, e5, Nf3, Nc6, Bb5)
      const whiteBtnIdx = children.indexOf(allButtons[4])
      // a6 is the 6th move button (after the split, in black-only row)
      const blackBtnIdx = children.indexOf(allButtons[5])

      expect(varLineIdx).toBeGreaterThan(whiteBtnIdx)
      expect(varLineIdx).toBeLessThan(blackBtnIdx)
    })

    it('whiteIdx branch shows placeholder dots after white cell', () => {
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const tree = makeTree([varNode], new Map([[4, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={0}
          onNavigate={noop}
          variationTree={tree}
          getAbsolutePly={makeGetAbsolutePly(tree)}
          onVarSelect={noop}
        />,
      )

      const dots = container.querySelectorAll('.move-placeholder-dots')
      expect(dots.length).toBeGreaterThan(0)
    })

    it('same pair with both whiteIdx and blackIdx branches orders correctly', () => {
      // whiteIdx=4 branch (black-first var) and blackIdx=5 branch (white-first var)
      const v1 = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
      const v2 = makeNode({ id: 'v2', san: 'Ba4', parentGameIndex: 5 })
      const tree = makeTree([v1, v2], new Map([[4, ['v1']], [5, ['v2']]]))

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={0}
          onNavigate={noop}
          variationTree={tree}
          getAbsolutePly={makeGetAbsolutePly(tree)}
          onVarSelect={noop}
        />,
      )

      const grid = container.querySelector('.move-list-grid')!
      const children = Array.from(grid.children)
      const varLines = children.filter(el => el.classList.contains('variation-line'))

      // Should have 2 variation lines
      expect(varLines.length).toBe(2)

      // v1 (whiteIdx branch, Nf6) should come before v2 (blackIdx branch, Ba4)
      const v1Idx = children.indexOf(varLines[0])
      const v2Idx = children.indexOf(varLines[1])
      expect(varLines[0].textContent).toContain('Nf6')
      expect(varLines[1].textContent).toContain('Ba4')
      expect(v1Idx).toBeLessThan(v2Idx)

      // Black cell (a6) should be between the two variation lines
      const allButtons = children.filter(el => el.classList.contains('move-button'))
      const blackBtnIdx = children.indexOf(allButtons[5]) // a6
      expect(blackBtnIdx).toBeGreaterThan(v1Idx)
      expect(blackBtnIdx).toBeLessThan(v2Idx)
    })

    it('no trailing black-only row when branching from final white move with no black', () => {
      // 7 moves: last pair has white only (no black)
      const moves = [
        ...GAME_MOVES,
        { san: 'Re1', eval: 60 },
      ]
      // Branch at whiteIdx=6 (Re1, the final white-only move)
      const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 6 })
      const tree = makeTree([varNode], new Map([[6, ['v1']]]))

      const { container } = render(
        <MoveList
          moves={moves}
          currentIndex={0}
          onNavigate={noop}
          variationTree={tree}
          getAbsolutePly={makeGetAbsolutePly(tree)}
          onVarSelect={noop}
        />,
      )

      // Should have variation line but no empty placeholder row after it
      const grid = container.querySelector('.move-list-grid')!
      const children = Array.from(grid.children)
      const varLineIdx = children.findIndex(el => el.classList.contains('variation-line'))
      expect(varLineIdx).toBeGreaterThan(-1)

      // After the variation line, there should be no move-button-placeholder pair
      // (i.e., no black-only split row with empty cells)
      const afterVar = children.slice(varLineIdx + 1)
      const emptyPlaceholders = afterVar.filter(
        el => el.classList.contains('move-button-placeholder') && !el.classList.contains('move-placeholder-dots'),
      )
      // There should be 0 trailing placeholders from a black-only row
      // (Other pairs may have placeholders, but we just check no empty row right after the var line)
      const firstAfterVar = afterVar[0]
      // The first element after the variation line should NOT be an empty move-number span
      // that would indicate a black-only split row
      if (firstAfterVar) {
        const isEmptyMoveNumber = firstAfterVar.classList.contains('move-number') && !firstAfterVar.textContent
        expect(isEmptyMoveNumber).toBe(false)
      }
    })

    it('click on split black cell fires onNavigate with correct index', () => {
      // Branch at whiteIdx=2 (Nf3) so black cell Nc6 (blackIdx=3) is in split row
      // Nc6 is NOT the last move, so onNavigate gets the raw index (not null)
      const varNode = makeNode({ id: 'v1', san: 'Bc5', parentGameIndex: 2 })
      const tree = makeTree([varNode], new Map([[2, ['v1']]]))
      const onNavigate = vi.fn()

      const { container } = render(
        <MoveList
          moves={GAME_MOVES}
          currentIndex={0}
          onNavigate={onNavigate}
          variationTree={tree}
          getAbsolutePly={makeGetAbsolutePly(tree)}
          onVarSelect={noop}
        />,
      )

      // Find the black cell for Nc6 (blackIdx=3) — it's in the split black-only row
      const allButtons = container.querySelectorAll('.move-button')
      // Nc6 is the 4th button (index 3)
      const blackBtn = allButtons[3] as HTMLButtonElement
      expect(blackBtn.textContent).toContain('Nc6')
      blackBtn.click()

      expect(onNavigate).toHaveBeenCalledWith(3)
    })
  })

  it('bubble messages and whiteIdx variation coexist with correct split layout', () => {
    const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
    const tree = makeTree([varNode], new Map([[4, ['v1']]]))

    // Bubble on white move at index 4 (Bb5)
    const messages = new Map([
      [4, [{ key: 'msg1', variant: 'srs-pass' as const, text: 'Nice!' }]],
    ])

    const { container } = render(
      <MoveList
        moves={GAME_MOVES}
        currentIndex={4}
        onNavigate={noop}
        variationTree={tree}
        getAbsolutePly={makeGetAbsolutePly(tree)}
        onVarSelect={noop}
        messages={messages}
      />,
    )

    const grid = container.querySelector('.move-list-grid')!
    const children = Array.from(grid.children)

    // All three should render
    const bubbles = container.querySelectorAll('.move-bubble')
    const variationLines = container.querySelectorAll('.variation-line')
    expect(bubbles.length).toBeGreaterThan(0)
    expect(variationLines.length).toBe(1)

    // Order: bubble before variation, variation before black cell
    const bubbleIdx = children.findIndex(el => el.classList.contains('move-bubble'))
    const varLineIdx = children.findIndex(el => el.classList.contains('variation-line'))
    const allButtons = children.filter(el => el.classList.contains('move-button'))
    const blackBtnIdx = children.indexOf(allButtons[5]) // a6
    expect(bubbleIdx).toBeLessThan(varLineIdx)
    expect(varLineIdx).toBeLessThan(blackBtnIdx)
  })

  it('renders variation lines without crashing when getAbsolutePly is missing', () => {
    const varNode = makeNode({ id: 'v1', san: 'Nf6', parentGameIndex: 4 })
    const tree = makeTree([varNode], new Map([[4, ['v1']]]))

    // No getAbsolutePly provided — should not crash, just skip variation rendering
    const { container } = render(
      <MoveList
        moves={GAME_MOVES}
        currentIndex={2}
        onNavigate={noop}
        variationTree={tree}
      />,
    )

    // Variation lines should not render (missing getAbsolutePly)
    expect(container.querySelectorAll('.variation-line').length).toBe(0)
    // But MoveRows should still render
    expect(container.querySelectorAll('.move-button').length).toBeGreaterThan(0)
  })
})
