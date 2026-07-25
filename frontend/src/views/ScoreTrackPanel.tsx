import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type ScoreResult, type TrackPoint } from '../api'

/** Two self-contained demo tracks with synthesised timestamps, so the panel exercises the
 *  served flagship model without needing per-point timestamps from the read-only API. A
 *  straight cruise (ordinary pattern of life) vs a tight orbit (the anomalous shape). */
function cruise(): TrackPoint[] {
  return Array.from({ length: 40 }, (_, k) => ({
    lat: 1.2 + 0.02 * k,
    lon: 103.6,
    alt_ft: 36000,
    ts: k * 30,
  }))
}
function orbit(): TrackPoint[] {
  return Array.from({ length: 40 }, (_, k) => {
    const a = 0.35 * k
    return { lat: 1.3 + 0.08 * Math.sin(a), lon: 103.9 + 0.08 * Math.cos(a), alt_ft: 12000, ts: k * 30 }
  })
}

export default function ScoreTrackPanel() {
  const model = useQuery({ queryKey: ['model'], queryFn: api.modelInfo })
  const [result, setResult] = useState<{ label: string; r: ScoreResult } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function score(label: string, points: TrackPoint[]) {
    setBusy(true)
    setError(null)
    try {
      setResult({ label, r: await api.scoreTrack(points) })
    } catch (err) {
      setResult(null)
      setError(err instanceof Error ? err.message : 'scoring failed')
    } finally {
      setBusy(false)
    }
  }

  const m = model.data
  return (
    <div className="det-card flagship">
      <h4>Try the flagship model</h4>
      {model.isPending && <p className="dim">checking the served model…</p>}
      {model.isError && <p className="caveat">model status is currently unavailable.</p>}
      {m && (
        <p className="dim" style={{ fontSize: 12 }}>
          served model: <b>{m.flagship}</b> ·{' '}
          {m.model_source === 'trained-artifact' ? (
            <>frozen artifact <code>{(m.artifact_sha256 ?? '').slice(0, 12)}…</code></>
          ) : (
            <>runtime fallback (no frozen artifact)</>
          )}
        </p>
      )}
      <p>
        The GRU scores a track by reconstruction error — a shape it can't reproduce is far
        from the learned pattern of life. Score two tracks against the same frozen model:
      </p>
      <div className="filter-row">
        <button type="button" disabled={busy} onClick={() => score('straight cruise', cruise())}>
          score a straight cruise
        </button>
        <button type="button" disabled={busy} onClick={() => score('tight orbit', orbit())}>
          score an orbiting track
        </button>
      </div>
      {error && <p className="caveat" role="alert">{error}</p>}
      {result && (
        <div className="evidence" aria-live="polite">
          {result.r.scored ? (
            <table>
              <tbody>
                <tr><td>track</td><td>{result.label}</td></tr>
                <tr><td>reconstruction error</td><td>{result.r.reconstruction_error?.toFixed(4)}</td></tr>
                <tr><td>population threshold</td><td>{result.r.population_threshold?.toFixed(4)}</td></tr>
                <tr>
                  <td>verdict</td>
                  <td style={{ color: result.r.anomalous ? 'var(--red)' : 'var(--green)' }}>
                    {result.r.anomalous ? 'above threshold — unusual (human review)' : 'within the population'}
                  </td>
                </tr>
              </tbody>
            </table>
          ) : (
            <span>no frozen model available on this deployment ({result.r.detail})</span>
          )}
        </div>
      )}
      <p className="caveat">
        An unusual shape is human-review decision support, never a verdict — a survey grid or
        holding pattern reconstructs poorly too.
      </p>
    </div>
  )
}
