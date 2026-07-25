import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import Sparkline from './Sparkline'

export default function IntegrityEvidence({ icao24 }: { icao24: string }) {
  const track = useQuery({
    queryKey: ['aircraft-track', icao24],
    queryFn: () => api.aircraftTrack(icao24),
  })

  if (track.isPending) return <p className="sub">loading integrity history…</p>
  if (track.isError || !track.data) {
    return <p className="caveat">No aligned integrity history is available for this aircraft.</p>
  }

  const p = track.data.properties
  const first = p.ts_series[0]
  const last = p.ts_series[p.ts_series.length - 1]
  return (
    <section className="integrity">
      <div className="integrity-head">
        <div>
          <span className="drawer-kicker">aircraft integrity witness</span>
          <h3>{p.icao24}</h3>
        </div>
        <span>{p.n} reports</span>
      </div>
      <Sparkline values={p.nic_series} label="NIC" color="var(--indigo-bright)" />
      <Sparkline values={p.nac_p_series} label="NACp" color="var(--sky)" />
      {first && last && (
        <p className="series-range">
          {new Date(first).toISOString()} → {new Date(last).toISOString()}
        </p>
      )}
    </section>
  )
}
