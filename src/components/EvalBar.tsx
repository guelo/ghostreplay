import { memo } from "react"
import { formatWhiteEval } from "./MoveRow.helpers"
import "./EvalBar.css"

type EvalBarProps = {
  whitePerspectiveCp: number | null
  whitePerspectiveMate?: number | null
  whiteOnBottom: boolean
  className?: string
}

const EVAL_BAR_CLAMP_CP = 1000

const clampEvalCp = (cp: number) =>
  Math.max(-EVAL_BAR_CLAMP_CP, Math.min(EVAL_BAR_CLAMP_CP, cp))

const toWhiteWinProbability = (cp: number) => {
  const clamped = clampEvalCp(cp)
  return 1 / (1 + 10 ** (-clamped / 400))
}

const EvalBar = ({
  whitePerspectiveCp,
  whitePerspectiveMate = null,
  whiteOnBottom,
  className = '',
}: EvalBarProps) => {
  // Label: '#' for checkmate (mate 0), 'M{n}'/'−M{n}' for mate-in-N, else cp.
  const evalLabel =
    whitePerspectiveMate !== null || whitePerspectiveCp !== null
      ? formatWhiteEval(whitePerspectiveCp, whitePerspectiveMate)
      : '--'

  // A non-zero mate is decisive (full bar). Otherwise — a plain cp eval OR a
  // mate-0 checkmate — fall back to the cp channel, which encodes the winner
  // (mate-0 callers supply an extreme ±cp so the bar fills toward the winner).
  const useMateSign =
    whitePerspectiveMate !== null && whitePerspectiveMate !== 0
  const signCp = useMateSign ? null : whitePerspectiveCp

  const evalTone = useMateSign
    ? whitePerspectiveMate! > 0
      ? 'positive'
      : 'negative'
    : signCp === null
      ? 'neutral'
      : signCp > 0
        ? 'positive'
        : signCp < 0
          ? 'negative'
          : 'neutral'

  const whiteFillPercent = (() => {
    if (useMateSign) return whitePerspectiveMate! > 0 ? 100 : 0
    if (signCp === null) return 50
    return toWhiteWinProbability(signCp) * 100
  })()

  return (
    <div className={`eval-bar ${className}`.trim()}>
      <span className={`eval-bar__value eval-bar__value--${evalTone}`}>
        {evalLabel}
      </span>
      <div
        className={`eval-bar__track ${whiteOnBottom ? 'eval-bar__track--white-bottom' : 'eval-bar__track--white-top'}`}
        role="img"
        aria-label={`Evaluation ${evalLabel}`}
      >
        <div
          className="eval-bar__white-fill"
          style={{ height: `${whiteFillPercent}%` }}
        />
      </div>
    </div>
  )
}

export default memo(EvalBar)
