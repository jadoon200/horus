interface SparklineProps {
  values: (number | null)[]
  label: string
  color: string
}

const WIDTH = 360
const HEIGHT = 104
const LEFT = 24
const RIGHT = 6
const TOP = 8
const BOTTOM = 18
const MIN = 0
const MAX = 11

function segments(values: (number | null)[]): string[] {
  const usableWidth = WIDTH - LEFT - RIGHT
  const usableHeight = HEIGHT - TOP - BOTTOM
  const x = (index: number) =>
    LEFT + (values.length <= 1 ? 0 : (index / (values.length - 1)) * usableWidth)
  const y = (value: number) => TOP + ((MAX - value) / (MAX - MIN)) * usableHeight

  const out: string[] = []
  let points: string[] = []
  values.forEach((value, index) => {
    if (value == null) {
      if (points.length) out.push(points.join(' '))
      points = []
      return
    }
    points.push(`${x(index).toFixed(2)},${y(Math.max(MIN, Math.min(MAX, value))).toFixed(2)}`)
  })
  if (points.length) out.push(points.join(' '))
  return out
}

/** Navigation-integrity series with a fixed 0–11 scale. Nulls split the line; they never
 * become zero, and the fixed scale prevents a one-category dip from looking catastrophic. */
export default function Sparkline({ values, label, color }: SparklineProps) {
  const lines = segments(values)
  return (
    <figure className="spark">
      <figcaption>
        <b>{label}</b>
        <span>fixed scale 0–11 · gaps = not reported</span>
      </figcaption>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${label} over time`}>
        {[0, 5, 11].map((tick) => {
          const y = TOP + ((MAX - tick) / (MAX - MIN)) * (HEIGHT - TOP - BOTTOM)
          return (
            <g key={tick}>
              <line x1={LEFT} x2={WIDTH - RIGHT} y1={y} y2={y} className="spark-grid" />
              <text x={LEFT - 5} y={y + 3} textAnchor="end">{tick}</text>
            </g>
          )
        })}
        {lines.map((points, index) => (
          <polyline
            key={`${label}-${index}`}
            points={points}
            fill="none"
            stroke={color}
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {lines.length === 0 && (
          <text x={WIDTH / 2} y={HEIGHT / 2} textAnchor="middle" className="spark-empty">
            no reported values
          </text>
        )}
      </svg>
    </figure>
  )
}
