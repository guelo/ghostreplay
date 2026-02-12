type EvalBarProps = {
  whitePerspectiveCp: number | null
  whitePerspectiveMate?: number | null
  whiteOnBottom: boolean
  className?: string
}

const EVAL_BAR_CLAMP_CP = 1000

const clampEvalCp = (cp: number) =>
  Math.max(-EVAL_BAR_CLAMP_CP, Math.min(EVAL_BAR_CLAMP_CP, cp))

const formatEvalCp = (cp: number): string => {
  const value = cp / 100
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}`
}

const formatEvalMate = (mate: number): string =>
  mate >= 0 ? `M${mate}` : `-M${Math.abs(mate)}`

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
  const evalLabel =
    whitePerspectiveMate !== null
      ? formatEvalMate(whitePerspectiveMate)
      : whitePerspectiveCp !== null
        ? formatEvalCp(whitePerspectiveCp)
        : '--'
  const evalTone =
    whitePerspectiveMate !== null
      ? whitePerspectiveMate > 0
        ? 'positive'
        : whitePerspectiveMate < 0
          ? 'negative'
          : 'neutral'
      : whitePerspectiveCp !== null
        ? whitePerspectiveCp > 0
          ? 'positive'
          : whitePerspectiveCp < 0
            ? 'negative'
            : 'neutral'
        : 'neutral'

  const whiteFillPercent = (() => {
    if (whitePerspectiveMate !== null) {
      if (whitePerspectiveMate > 0) return 100
      if (whitePerspectiveMate < 0) return 0
      return 50
    }
    if (whitePerspectiveCp === null) return 50
    return toWhiteWinProbability(whitePerspectiveCp) * 100
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

export default EvalBar
