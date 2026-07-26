import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, reliabilityMeta, type Incident } from '../api'

const DETECTORS = ['all', 'jamming', 'gap', 'incursion', 'spoof', 'squawk', 'anomaly'] as const

function IncidentCard({ i }: { i: Incident }) {
  const [open, setOpen] = useState(false)
  const meta = reliabilityMeta(i.reliability)
  return (
    <div
      className="roll"
      role="button"
      tabIndex={0}
      aria-expanded={open}
      onClick={() => setOpen(!open)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          setOpen(!open)
        }
      }}
    >
      <div className="roll-head">
        <div className={`rating ${meta.tone}`} title={meta.label}>
          <span className="rv">{i.reliability}</span>
          <span className="rk">Admiralty</span>
        </div>
        <span className="roll-id">{i.icao24 ?? '(area)'} · {i.incident_type}</span>
        <span className={`chip sev-${i.severity}`}>{i.severity}</span>
        <span className="risk">{i.score.toFixed(2)}</span>
      </div>
      <div className="chips">
        <span className="chip det">{i.detector}</span>
        {i.techniques.map((t) => (
          <span key={t} className="chip tech">{t}</span>
        ))}
        {i.zone && <span className="chip">{i.zone}</span>}
        <span className="chip">{new Date(i.ts_start).toISOString().slice(0, 16)}Z</span>
      </div>
      {open && (
        <div className="evidence">
          <table>
            <tbody>
              {Object.entries(i.evidence).map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function Incidents() {
  const [detector, setDetector] = useState<(typeof DETECTORS)[number]>('all')
  const incidents = useQuery({
    queryKey: ['incidents', detector],
    queryFn: () => api.incidents(detector === 'all' ? undefined : detector),
  })
  return (
    <div>
      <div className="filter-row">
        {DETECTORS.map((d) => (
          <button key={d} className={detector === d ? 'on' : ''} onClick={() => setDetector(d)}>
            {d}
          </button>
        ))}
      </div>
      <section className="panel">
        <h3>Incident feed</h3>
        <p className="sub">
          Ranked by detector score. Every row is decision support for human review — the
          Admiralty grade states how far the ADS-B evidence itself can be trusted, and the
          expandable evidence table shows exactly which thresholds fired.
        </p>
        {incidents.isLoading && <p className="sub">loading…</p>}
        {(incidents.data ?? []).map((i) => (
          <IncidentCard key={i.incident_id} i={i} />
        ))}
        {incidents.data && incidents.data.length === 0 && (
          <p className="sub">No incidents for this filter.</p>
        )}
      </section>
    </div>
  )
}
