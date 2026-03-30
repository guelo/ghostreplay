import type { ReactElement } from 'react'
import type { VariationNodeId, VarNode, VariationTree } from '../types/variationTree'

export type VariationLineProps = {
  rootNodeId: VariationNodeId
  tree: VariationTree
  selectedNodeId: VariationNodeId | null
  onNodeClick: (nodeId: VariationNodeId) => void
  getAbsolutePly: (nodeId: VariationNodeId) => number
  showPrefix: boolean
  /** Number of "|  " segments before "|- " in the prefix. Default 0. */
  depth?: number
}

/**
 * Render a single ply span with optional move number.
 * Move number is inside the .variation-ply span so selection highlights both.
 */
function renderPly(
  node: VarNode,
  isFirstInLine: boolean,
  selectedNodeId: VariationNodeId | null,
  onNodeClick: (nodeId: VariationNodeId) => void,
  getAbsolutePly: (nodeId: VariationNodeId) => number,
): ReactElement {
  const absPly = getAbsolutePly(node.id)
  const fullmove = Math.floor(absPly / 2) + 1
  const isBlack = absPly % 2 === 1
  const isSelected = node.id === selectedNodeId

  // Show move number for: first ply of the line (always), or white's turn
  const showNumber = isFirstInLine || !isBlack

  let numberStr = ''
  if (showNumber) {
    numberStr = isBlack ? `${fullmove}...` : `${fullmove}. `
  }

  return (
    <span
      key={node.id}
      className={`variation-ply${isSelected ? ' variation-ply--selected' : ''}`}
      data-node-id={node.id}
      onClick={() => onNodeClick(node.id)}
    >
      {showNumber && (
        <span className="variation-move-number">{numberStr}</span>
      )}
      {node.san}
    </span>
  )
}

/**
 * Renders a variation branch as inline notation text.
 * Returns a Fragment so all <div>s are direct children of the CSS grid.
 */
function VariationLine({
  rootNodeId,
  tree,
  selectedNodeId,
  onNodeClick,
  getAbsolutePly,
  showPrefix,
  depth = 0,
}: VariationLineProps): ReactElement {
  // 1. Collect inline plies following children[0] until branch or dead end
  const inlinePlies: VarNode[] = []
  let currentId: VariationNodeId | null = rootNodeId
  while (currentId) {
    const node = tree.nodes.get(currentId)
    if (!node) break
    inlinePlies.push(node)
    if (node.children.length !== 1) break // dead end or branch point
    currentId = node.children[0]
  }

  if (inlinePlies.length === 0) return <></>

  const lastNode = inlinePlies[inlinePlies.length - 1]

  // 2. Child lines from branch point (all children rendered, continuation first)
  const childLines: ReactElement[] = []
  if (lastNode.children.length > 1) {
    // All siblings share the same depth; increment only if the current line already has a prefix
    const childDepth = showPrefix ? depth + 1 : depth
    for (const childId of lastNode.children) {
      childLines.push(
        <VariationLine
          key={childId}
          rootNodeId={childId}
          tree={tree}
          selectedNodeId={selectedNodeId}
          onNodeClick={onNodeClick}
          getAbsolutePly={getAbsolutePly}
          showPrefix={true}
          depth={childDepth}
        />,
      )
    }
  }

  // 3. Fragment: this line div + child line elements (siblings in grid)
  return (
    <>
      <div
        className="variation-line"
      >
        {showPrefix && <span className="variation-prefix">{'|  '.repeat(depth)}|- </span>}
        {inlinePlies.map((node, i) =>
          renderPly(node, i === 0, selectedNodeId, onNodeClick, getAbsolutePly),
        )}
      </div>
      {childLines}
    </>
  )
}

export default VariationLine
