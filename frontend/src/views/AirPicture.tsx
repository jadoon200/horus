import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CircleMarker, MapContainer, Polygon, Polyline, Rectangle, TileLayer, Tooltip } from 'react-leaflet'
import { api, reliabilityMeta, type AircraftRollup, type AreaIncident, type CoverageCell } from '../api'
import Drawer from '../components/Drawer'
import IntegrityEvidence from '../components/IntegrityEvidence'

/** Three states, never two. An unscoreable cell is drawn as hatched grey — visibly
 *  "no data", never as clear sky, because most of the map is unscoreable at Singapore
 *  traffic densities and hiding that would misrepresent the coverage. */
function cellStyle(c: CoverageCell) {
  if (!c.scoreable) {
    // Visibly "no data": hatched grey outline, barely filled. It must read as absence,
    // never as clear sky — and it is most of the map, so a heavy fill would swamp
    // everything the map is actually for.
    return { color: '#64748b', weight: 0.5, fillColor: '#334155', fillOpacity: 0.1, dashArray: '2 3' }
  }
  const f = c.degraded_fraction ?? 0
  if (f >= 0.5) return { color: '#f87171', weight: 1.2, fillColor: '#f87171', fillOpacity: 0.5 }
  if (f > 0) return { color: '#fbbf24', weight: 1, fillColor: '#fbbf24', fillOpacity: 0.3 }
  // "Clear" is the overwhelmingly common, uninteresting case — a hairline only. Filling it
  // buried the tracks and the degraded cells under a wash of green.
  return { color: '#4ade80', weight: 0.4, fillColor: '#4ade80', fillOpacity: 0.02 }
}

function riskTone(risk: number): string {
  return risk >= 0.8 ? 'high' : risk >= 0.6 ? 'mid' : 'low'
}

function Rating({ grade }: { grade: string }) {
  const meta = reliabilityMeta(grade)
  return (
    <div className={`rating ${meta.tone}`} title={`${meta.label} (NATO Admiralty, ADS-B confidence)`}>
      <span className="rv">{grade}</span>
      <span className="rk">Admiralty</span>
    </div>
  )
}

function RollupCard({ r, onOpen }: { r: AircraftRollup; onOpen: () => void }) {
  return (
    <div
      className="roll"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') onOpen()
      }}
    >
      <div className="roll-head">
        <Rating grade={r.reliability} />
        <span className="roll-id">{r.icao24}</span>
        <span className={`risk ${riskTone(r.risk)}`}>{r.risk.toFixed(2)}</span>
      </div>
      <div className="chips">
        {r.detectors.map((d) => (
          <span key={d} className="chip det">{d}</span>
        ))}
        {r.techniques.map((t) => (
          <span key={t} className="chip tech">{t}</span>
        ))}
        {r.zone && <span className="chip">{r.zone}</span>}
      </div>
    </div>
  )
}

function AreaCard({ a, onOpen }: { a: AreaIncident; onOpen: () => void }) {
  const degraded = a.evidence['aircraft_degraded'] as number | undefined
  const observed = a.evidence['aircraft_observed'] as number | undefined
  return (
    <div
      className="roll"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') onOpen()
      }}
    >
      <div className="roll-head">
        <Rating grade={a.reliability} />
        <span className="roll-id">{a.incident_type}</span>
        <span className={`chip sev-${a.severity}`}>{a.severity}</span>
        <span className={`risk ${riskTone(a.score)}`}>{a.score.toFixed(2)}</span>
      </div>
      <div className="chips">
        {a.zone && <span className="chip">{a.zone}</span>}
        <span className="chip">
          {degraded ?? '?'}/{observed ?? '?'} aircraft degraded
        </span>
        <span className="chip">{new Date(a.ts_start).toISOString().slice(11, 16)}Z</span>
      </div>
    </div>
  )
}

function RollupEvidence({ rollup }: { rollup: AircraftRollup }) {
  return (
    <>
      <div className="evidence drawer-table">
        <table>
          <tbody>
            <tr><td>best incident score</td><td>{rollup.risk_breakdown.best_incident_score.toFixed(3)}</td></tr>
            <tr><td>agreeing detectors</td><td>{rollup.risk_breakdown.agreeing_detectors} (+{rollup.risk_breakdown.agreement_bonus.toFixed(2)})</td></tr>
            <tr><td>sensitive-zone bonus</td><td>+{rollup.risk_breakdown.sensitive_zone_bonus.toFixed(2)}</td></tr>
            <tr><td>composite risk</td><td>{rollup.risk.toFixed(3)} — transparent sum, capped at 1</td></tr>
            <tr><td>incidents</td><td>{rollup.incidents.join(', ')}</td></tr>
          </tbody>
        </table>
      </div>
      <IntegrityEvidence icao24={rollup.icao24} />
    </>
  )
}

/** Plain-language phase-of-flight read on a jamming cell's degraded aircraft.
 *
 * The signature — many aircraft degraded together in one place — is also what an arrival
 * stream looks like, so a reviewer needs to know at a glance whether the population was at
 * cruise or on approach. The detector never filters on this; it only reports it.
 */
function phaseOfFlightNote(area: AreaIncident): string | null {
  const num = (key: string): number | null =>
    typeof area.evidence[key] === 'number' ? (area.evidence[key] as number) : null
  const known = num('degraded_altitude_known')
  const terminal = num('degraded_in_terminal_phase')
  const floor = num('terminal_alt_ft')
  const lo = num('degraded_alt_ft_min')
  const hi = num('degraded_alt_ft_max')
  if (known === null || terminal === null || floor === null || !known) return null

  const ft = (v: number | null) => (v === null ? '?' : v.toLocaleString())
  const band = lo === hi ? `${ft(lo)} ft` : `${ft(lo)}–${ft(hi)} ft`
  const limit = floor.toLocaleString()
  if (terminal === 0) {
    return `Phase of flight: all ${known} degraded aircraft were above ${limit} ft (${band}) — cruise traffic, not an arrival stream sharing a benign local cause.`
  }
  if (terminal === known) {
    return `Phase of flight: all ${known} degraded aircraft were below ${limit} ft (${band}) — weigh a shared approach/departure cause before reading this as interference.`
  }
  return `Phase of flight: ${terminal} of ${known} degraded aircraft were below ${limit} ft (${band}) — a mixed population; check whether the low ones share an approach path.`
}

function AreaEvidence({ area }: { area: AreaIncident }) {
  const rawWorst = area.evidence['worst_nic_by_aircraft']
  const worst =
    rawWorst && typeof rawWorst === 'object' && !Array.isArray(rawWorst)
      ? Object.entries(rawWorst).filter((entry): entry is [string, number] => typeof entry[1] === 'number')
      : []
  const hardLoss = new Set(
    Array.isArray(area.evidence['hard_loss_aircraft'])
      ? area.evidence['hard_loss_aircraft'].filter((value): value is string => typeof value === 'string')
      : [],
  )
  const [selected, setSelected] = useState<string | null>(worst[0]?.[0] ?? null)
  const phase = phaseOfFlightNote(area)

  return (
    <>
      {phase && <p className="drawer-note">{phase}</p>}
      <div className="evidence drawer-table">
        <table>
          <tbody>
            {Object.entries(area.evidence)
              .filter(([key]) => !['worst_nic_by_aircraft', 'hard_loss_aircraft'].includes(key))
              .map(([key, value]) => (
                <tr key={key}>
                  <td>{key}</td>
                  <td>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      <section className="witnesses">
        <span className="drawer-kicker">corroborating aircraft</span>
        <p>
          A regional signal requires several independent aircraft in the same cell-window.
          Select one witness to inspect its aligned NIC and NACp history.
        </p>
        <div className="witness-list">
          {worst.map(([icao24, nic]) => (
            <button
              type="button"
              key={icao24}
              className={selected === icao24 ? 'selected' : ''}
              onClick={() => setSelected(icao24)}
            >
              <span>{icao24}</span>
              <b>NIC {nic}</b>
              {hardLoss.has(icao24) && <i>NIC+NACp hard loss</i>}
            </button>
          ))}
        </div>
      </section>
      {selected && <IntegrityEvidence icao24={selected} />}
    </>
  )
}

export default function AirPicture() {
  const picture = useQuery({ queryKey: ['air-picture'], queryFn: api.airPicture })
  const coverage = useQuery({ queryKey: ['coverage'], queryFn: () => api.coverage(6) })
  const [showCoverage, setShowCoverage] = useState(true)
  const zones = useQuery({ queryKey: ['zones'], queryFn: api.zones })
  const tracks = useQuery({ queryKey: ['tracks'], queryFn: api.tracks })
  const [drawer, setDrawer] = useState<
    { kind: 'area'; area: AreaIncident } | { kind: 'aircraft'; rollup: AircraftRollup } | null
  >(null)

  const trackLines = useMemo(
    () =>
      (tracks.data?.features ?? []).map((f) => ({
        id: String(f.properties['track_id']),
        // GeoJSON is [lon, lat]; Leaflet wants [lat, lon].
        latlngs: (f.geometry.coordinates as [number, number][]).map(
          ([lon, lat]) => [lat, lon] as [number, number],
        ),
      })),
    [tracks.data],
  )

  // Distinct resolutions actually drawn, finest first. The grid is multi-resolution — a cell
  // too sparse to judge is retried doubled — so the map mixes box sizes on purpose; say so
  // rather than leaving a viewer to read a big box as a bigger outage.
  const cellSizes = useMemo(
    () => [...new Set((coverage.data?.cells ?? []).map((c) => c.cell_deg))].sort((a, b) => a - b),
    [coverage.data],
  )

  const cov = coverage.data
  return (
    <div className="split">
      <div>
        <div className="cov-bar">
          <label className="cov-toggle">
            <input
              type="checkbox"
              checked={showCoverage}
              onChange={(e) => setShowCoverage(e.target.checked)}
            />
            GNSS coverage
          </label>
          <span className="key"><i className="sw clear" /> clear</span>
          <span className="key"><i className="sw part" /> some degraded</span>
          <span className="key"><i className="sw bad" /> majority degraded</span>
          <span className="key"><i className="sw none" /> unscoreable</span>
          {cov && (
            <span className="cov-count">
              {/* A baked snapshot is aggregated whole, so the API reports no window and the
                  caption must not invent one — it used to read "worst over 6 h" over a demo
                  seed that was never filtered by time. */}
              {cov.window_hours === null
                ? 'worst over the whole snapshot'
                : `worst over ${cov.window_hours} h`}{' '}
              · {cov.cells_unscoreable}/{cov.cells_total} unscoreable — fewer than{' '}
              {cov.min_aircraft} aircraft, so not judged
            </span>
          )}
          {cellSizes.length > 1 && (
            <span className="cov-count">
              box size = scoring resolution ({cellSizes.map((d) => `${d}°`).join(' · ')}): the
              grid starts fine and is doubled where too few aircraft flew to judge it, so a big
              box is a coarser answer, not a bigger outage
            </span>
          )}
        </div>
      <div className="map-panel">
        <MapContainer center={[1.25, 103.85]} zoom={8} scrollWheelZoom>
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          />
          {(zones.data?.features ?? []).map((f) => {
            const ring = (f.geometry.coordinates as [number, number][][])[0]
            const latlngs = ring.map(([lon, lat]) => [lat, lon] as [number, number])
            const sensitive = Boolean(f.properties['sensitive'])
            return (
              <Polygon
                key={String(f.properties['zone_id'])}
                positions={latlngs}
                pathOptions={{
                  color: sensitive ? '#fbbf24' : '#38bdf8',
                  weight: 1,
                  fillOpacity: 0.04,
                  dashArray: '4 5',
                }}
              >
                <Tooltip sticky>
                  {String(f.properties['name'])} ({String(f.properties['kind'])})
                </Tooltip>
              </Polygon>
            )
          })}
          {showCoverage &&
            // Coarsest first: a coarse cell spans sky that finer cells also cover, so
            // drawing it last would paint over the more precise answer beneath it.
            [...(coverage.data?.cells ?? [])]
              .sort((a, b) => b.cell_deg - a.cell_deg)
              .map((c, i) => (
              <Rectangle
                key={`cov-${i}`}
                bounds={[
                  [c.lat - c.cell_deg / 2, c.lon - c.cell_deg / 2],
                  [c.lat + c.cell_deg / 2, c.lon + c.cell_deg / 2],
                ]}
                pathOptions={cellStyle(c)}
              >
                <Tooltip sticky>
                  {c.scoreable ? (
                    <>
                      <b>Worst in window:</b>{' '}
                      {((c.degraded_fraction ?? 0) * 100).toFixed(0)}% of up to{' '}
                      {c.aircraft_observed} aircraft degraded
                      {c.hard_loss > 0 && <> · {c.hard_loss} in hard loss</>}
                      <br />
                      cell {c.cell_deg}° · {c.windows} time window
                      {c.windows === 1 ? '' : 's'} · not the current state
                    </>
                  ) : (
                    <>
                      <b>Unscoreable</b> — at most {c.aircraft_observed} aircraft seen here
                      <br />
                      fewer than the minimum to judge; deliberately not scored
                      <br />
                      cell {c.cell_deg}°
                    </>
                  )}
                </Tooltip>
              </Rectangle>
              ))}
          {trackLines.map((t) => (
            <Polyline
              key={t.id}
              positions={t.latlngs}
              pathOptions={{ color: '#4c5a85', weight: 1, opacity: 0.6 }}
            />
          ))}
          {(picture.data?.areas ?? []).map(
            (a) =>
              a.lat != null &&
              a.lon != null && (
                <CircleMarker
                  key={a.incident_id}
                  center={[a.lat, a.lon]}
                  radius={26}
                  pathOptions={{ color: '#f87171', weight: 2, fillOpacity: 0.15 }}
                  eventHandlers={{ click: () => setDrawer({ kind: 'area', area: a }) }}
                >
                  <Tooltip sticky>
                    {a.incident_type}: {String(a.evidence['aircraft_degraded'])}/
                    {String(a.evidence['aircraft_observed'])} aircraft degraded
                  </Tooltip>
                </CircleMarker>
              ),
          )}
        </MapContainer>
      </div>
      </div>
      <div>
        <section className="panel">
          <h3>Area signals — GNSS interference</h3>
          <p className="sub">
            Cell-level integrity collapse across many aircraft. Unscoreable cells (too few
            aircraft) are skipped, never scored.
          </p>
          {picture.isLoading && <p className="sub">loading…</p>}
          {(picture.data?.areas ?? []).map((a) => (
            <AreaCard
              key={a.incident_id}
              a={a}
              onOpen={() => setDrawer({ kind: 'area', area: a })}
            />
          ))}
          {picture.data && picture.data.areas.length === 0 && (
            <p className="sub">No area-level incidents in the current picture.</p>
          )}
        </section>
        <section className="panel">
          <h3>Aircraft rollups</h3>
          <p className="sub">
            Which detectors agree, unioned technique tags, and a transparent risk sum —
            click a row for the arithmetic.
          </p>
          {(picture.data?.aircraft ?? []).map((r) => (
            <RollupCard
              key={r.icao24}
              r={r}
              onOpen={() => setDrawer({ kind: 'aircraft', rollup: r })}
            />
          ))}
        </section>
      </div>
      {drawer && (
        <Drawer
          title={drawer.kind === 'area' ? drawer.area.incident_type : drawer.rollup.icao24}
          onClose={() => setDrawer(null)}
        >
          {drawer.kind === 'area' ? (
            <AreaEvidence key={drawer.area.incident_id} area={drawer.area} />
          ) : (
            <RollupEvidence rollup={drawer.rollup} />
          )}
        </Drawer>
      )}
    </div>
  )
}
