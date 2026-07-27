/**
 * Latency-by-move-index chart for a thermal run (§10.4: "use at least a 40-move
 * sequence and graph latency by move index").
 *
 * Inline SVG built as a string — no chart library on a page whose whole job is to
 * measure latency without perturbing it. Points carry a native `<title>` so a
 * value is readable on hover without a scripted overlay competing with the
 * measurement for main-thread time.
 *
 * ONE SERIES PER ARM. Pooling protocols into a single curve would hide the
 * difference the graph exists to show; with two or more series a legend is always
 * drawn and each curve is direct-labelled at its end, so identity is never
 * carried by colour alone. Colours come from `--series-0..2`, whose light and dark
 * steps are validated for CVD separation, lightness, chroma and contrast against
 * both page surfaces (see `bench/device/index.html`).
 */

export type ChartPoint = { thermalIndex: number; medianMs: number; n: number }
export type ChartSeries = { label: string; points: readonly ChartPoint[] }

const WIDTH = 720
const HEIGHT = 260
const PAD = { top: 18, right: 18, bottom: 34, left: 56 }
const LEGEND_HEIGHT = 20

/** Fixed slot order: a series keeps its colour when another one is added. */
const MAX_SERIES = 3

const escapeText = (value: string) =>
  value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/** Round to a readable axis top: 1/2/5 × 10^n above the observed maximum. */
const niceCeiling = (value: number): number => {
  if (!Number.isFinite(value) || value <= 0) return 1
  const magnitude = 10 ** Math.floor(Math.log10(value))
  const normalized = value / magnitude
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10
  return step * magnitude
}

const EMPTY =
  '<p class="muted">A thermal run of at least two moves draws the curve here.</p>'

export const renderLatencyChart = (series: readonly ChartSeries[]): string => {
  const drawable = series.filter((entry) => entry.points.length >= 2).slice(0, MAX_SERIES)
  if (drawable.length === 0) {
    return EMPTY
  }

  const allPoints = drawable.flatMap((entry) => [...entry.points])
  const maxY = niceCeiling(Math.max(...allPoints.map((point) => point.medianMs)))
  const indices = allPoints.map((point) => point.thermalIndex)
  const minX = Math.min(...indices)
  const maxX = Math.max(...indices)

  const multi = drawable.length > 1
  const top = PAD.top + (multi ? LEGEND_HEIGHT : 0)
  const plotW = WIDTH - PAD.left - PAD.right
  const plotH = HEIGHT - top - PAD.bottom

  const x = (index: number) =>
    PAD.left + (maxX === minX ? plotW / 2 : ((index - minX) / (maxX - minX)) * plotW)
  const y = (ms: number) => top + plotH - (ms / maxY) * plotH

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => fraction * maxY)
  const gridLines = ticks
    .map(
      (value) =>
        `<line class="grid" x1="${PAD.left}" x2="${WIDTH - PAD.right}" y1="${y(value).toFixed(1)}" y2="${y(value).toFixed(1)}" />` +
        `<text class="axis" x="${PAD.left - 8}" y="${(y(value) + 4).toFixed(1)}" text-anchor="end">${Math.round(value)}</text>`,
    )
    .join('')

  const xTickIndices = [...new Set(indices)]
    .sort((a, b) => a - b)
    .filter((index, position, all) => position === 0 || position === all.length - 1 || index % 10 === 0)
  const xTicks = xTickIndices
    .map(
      (index) =>
        `<text class="axis" x="${x(index).toFixed(1)}" y="${HEIGHT - PAD.bottom + 18}" text-anchor="middle">${index}</text>`,
    )
    .join('')

  const legend = multi
    ? drawable
        .map((entry, slot) => {
          const left = PAD.left + slot * 150
          return (
            `<line class="series series-${slot}" x1="${left}" x2="${left + 18}" y1="${PAD.top + 4}" y2="${PAD.top + 4}" />` +
            `<text class="legend" x="${left + 24}" y="${PAD.top + 8}">${escapeText(entry.label)}</text>`
          )
        })
        .join('')
    : ''

  const body = drawable
    .map((entry, slot) => {
      const points = [...entry.points].sort((a, b) => a.thermalIndex - b.thermalIndex)
      const path = points
        .map(
          (point, index) =>
            `${index === 0 ? 'M' : 'L'}${x(point.thermalIndex).toFixed(1)},${y(point.medianMs).toFixed(1)}`,
        )
        .join(' ')

      // A surface-coloured ring keeps overlapping series readable where two
      // curves cross.
      const dotClass = multi ? `dot dot-ringed series-${slot}` : `dot series-${slot}`
      const dots = points
        .map(
          (point) =>
            `<circle class="${dotClass}" cx="${x(point.thermalIndex).toFixed(1)}" cy="${y(point.medianMs).toFixed(1)}" r="2.5">` +
            `<title>${escapeText(`${entry.label} · move ${point.thermalIndex}: ${Math.round(point.medianMs)} ms (n=${point.n})`)}</title></circle>`,
        )
        .join('')

      const last = points[points.length - 1]
      const peak = points.reduce((worst, point) => (point.medianMs > worst.medianMs ? point : worst))
      const label = (point: ChartPoint, text: string, anchor: string, dx: number) =>
        `<text class="point-label" x="${(x(point.thermalIndex) + dx).toFixed(1)}" y="${(y(point.medianMs) - 8).toFixed(1)}" text-anchor="${anchor}">${escapeText(text)}</text>`

      // One series: label the peak and the endpoint, which is what a thermal read
      // is about. Several: label each curve's end with its arm, so the legend is
      // not the only way to tell them apart.
      const labels = multi
        ? label(last, entry.label, 'end', -6)
        : label(peak, `${Math.round(peak.medianMs)} ms`, 'middle', 0) +
          (peak.thermalIndex === last.thermalIndex
            ? ''
            : label(last, `${Math.round(last.medianMs)} ms`, 'end', -4))

      return `<path class="series series-${slot}" d="${path}" fill="none" />${dots}${labels}`
    })
    .join('')

  return (
    `<svg class="latency-chart" viewBox="0 0 ${WIDTH} ${HEIGHT}" role="img" ` +
    `aria-label="Median end-to-end analyze-move latency by move index${multi ? `, by arm: ${escapeText(drawable.map((entry) => entry.label).join(', '))}` : ''}">` +
    gridLines +
    xTicks +
    `<text class="axis-title" x="${PAD.left}" y="${HEIGHT - 4}">move index</text>` +
    legend +
    body +
    '</svg>'
  )
}
