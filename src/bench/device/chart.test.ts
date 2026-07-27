import { describe, expect, it } from 'vitest'
import { renderLatencyChart } from './chart'

const points = (values: number[]) =>
  values.map((medianMs, index) => ({ thermalIndex: index + 1, medianMs, n: 3 }))

describe('renderLatencyChart', () => {
  it('draws one curve per arm rather than pooling them into one', () => {
    const svg = renderLatencyChart([
      { label: 'current', points: points([100, 200]) },
      { label: 'variantA', points: points([300, 400]) },
    ])

    expect(svg.match(/<path/g)).toHaveLength(2)
    expect(svg).toContain('series-0')
    expect(svg).toContain('series-1')
  })

  it('names both arms in text, so identity never rests on colour alone', () => {
    const svg = renderLatencyChart([
      { label: 'current', points: points([100, 200]) },
      { label: 'variantA', points: points([300, 400]) },
    ])

    // A legend AND a direct label per curve: two mentions of each arm.
    expect(svg.match(/current/g)?.length).toBeGreaterThanOrEqual(2)
    expect(svg.match(/variantA/g)?.length).toBeGreaterThanOrEqual(2)
    expect(svg).toContain('aria-label')
  })

  it('omits the legend for a single series and labels its peak instead', () => {
    const svg = renderLatencyChart([{ label: 'current', points: points([100, 900, 400]) }])

    expect(svg).not.toContain('class="legend"')
    expect(svg).toContain('900 ms')
  })

  it('needs two points to draw anything', () => {
    expect(renderLatencyChart([])).toContain('at least two moves')
    expect(renderLatencyChart([{ label: 'current', points: points([100]) }])).toContain(
      'at least two moves',
    )
  })

  it('escapes a label rather than injecting it as markup', () => {
    const svg = renderLatencyChart([
      { label: '<script>x</script>', points: points([100, 200]) },
      { label: 'variantA', points: points([300, 400]) },
    ])

    expect(svg).not.toContain('<script>')
    expect(svg).toContain('&lt;script&gt;')
  })
})
