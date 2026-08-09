/** Typed client for the HORUS read-only air-domain-awareness API. */

// Same-origin in a production build (FastAPI serves this SPA and the API from one host);
// localhost:8000 in dev (`make ui` on :5199 talks to `make api` on :8000). VITE_API_URL overrides.
const BASE = import.meta.env.VITE_API_URL ?? (import.meta.env.PROD ? '' : 'http://localhost:8000')

export interface Health {
  status: string
  version: string
}

export interface Stats {
  aircraft: number
  positions: number
  tracks: number
  incidents: number
  incidents_by_detector: Record<string, number>
  demo_mode: boolean
  newest_report: string | null
  /** Age of the newest report. Drives the staleness banner — a dashboard that looks live
   *  while the collector is dead is worse than one that admits it. */
  data_age_seconds: number | null
}

/** One patch of sky and what could honestly be said about it. */
export interface CoverageCell {
  lat: number
  lon: number
  cell_deg: number
  level: number
  aircraft_observed: number
  scoreable: boolean
  /** null exactly when the cell is unscoreable — never 0, which would read as "clear". */
  degraded_fraction: number | null
  hard_loss: number
  /** How many time windows were collapsed into this map cell. The fraction shown is the
   *  WORST across them, so an event is never hidden by a later quiet window — which also
   *  means the colour is not the current state. */
  windows: number
}

export interface Coverage {
  /** Null when no window was applied — a baked demo snapshot is aggregated whole. */
  window_hours: number | null
  cell_windows: number
  cells_total: number
  cells_scoreable: number
  cells_unscoreable: number
  min_aircraft: number
  cells: CoverageCell[]
}

export interface Incident {
  incident_id: string
  icao24: string | null
  track_id: string | null
  detector: string
  incident_type: string
  score: number
  severity: string
  reliability: string // NATO-Admiralty ADS-B-confidence grade A-F
  ts_start: string
  ts_end: string | null
  lat: number | null
  lon: number | null
  zone: string | null
  affected_count: number | null
  techniques: string[]
  evidence: Record<string, unknown>
}

export interface AircraftRollup {
  icao24: string
  risk: number
  risk_breakdown: {
    best_incident_score: number
    agreeing_detectors: number
    agreement_bonus: number
    sensitive_zone_bonus: number
  }
  detectors: string[]
  techniques: string[]
  reliability: string
  incidents: string[]
  zone: string | null
  ts_start: string
}

export interface AreaIncident {
  incident_id: string
  incident_type: string
  score: number
  severity: string
  reliability: string
  lat: number | null
  lon: number | null
  zone: string | null
  affected_count: number | null
  ts_start: string
  ts_end: string | null
  evidence: Record<string, unknown>
}

export interface AirPicture {
  aircraft: AircraftRollup[]
  areas: AreaIncident[]
}

export interface GeoFeature {
  type: 'Feature'
  geometry: { type: string; coordinates: unknown }
  properties: Record<string, unknown>
}

export interface FeatureCollection {
  type: 'FeatureCollection'
  features: GeoFeature[]
}

export interface AircraftTrack {
  type: 'Feature'
  geometry: { type: 'LineString'; coordinates: [number, number, number][] }
  properties: {
    icao24: string
    n: number
    ts_series: string[]
    nic_series: (number | null)[]
    nac_p_series: (number | null)[]
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`)
  return res.json() as Promise<T>
}

export interface ModelInfo {
  flagship: string
  model_source: 'trained-artifact' | 'runtime-fallback'
  artifact_sha256: string | null
  sha_pinned: boolean
  sha_matches_pin: boolean | null
}

export interface TrackPoint {
  lat: number
  lon: number
  alt_ft: number | null
  ts: number
}

export interface ScoreResult {
  model_source: 'trained-artifact' | 'runtime-fallback'
  scored: boolean
  reconstruction_error?: number
  population_threshold?: number
  anomalous?: boolean
  artifact_sha256?: string
  detail?: string
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`)
  return res.json() as Promise<T>
}

export const api = {
  health: () => get<Health>('/health'),
  stats: () => get<Stats>('/stats'),
  incidents: (detector?: string) =>
    get<Incident[]>(`/incidents${detector ? `?detector=${detector}` : ''}`),
  airPicture: () => get<AirPicture>('/air-picture'),
  zones: () => get<FeatureCollection>('/zones'),
  coverage: (hours = 6) => get<Coverage>(`/gnss-coverage?hours=${hours}`),
  tracks: () => get<FeatureCollection>('/tracks'),
  aircraftTrack: (icao24: string) =>
    get<AircraftTrack>(`/aircraft/${encodeURIComponent(icao24)}/track`),
  modelInfo: () => get<ModelInfo>('/model'),
  scoreTrack: (points: TrackPoint[]) => post<ScoreResult>('/score-track', { points }),
}

/** Reliability grade → tone + human label (NATO Admiralty system, ADS-B confidence). */
export function reliabilityMeta(grade: string): { label: string; tone: string } {
  const labels: Record<string, string> = {
    A: 'Completely reliable',
    B: 'Usually reliable',
    C: 'Fairly reliable',
    D: 'Not usually reliable',
    E: 'Unreliable',
    F: 'Cannot be judged',
  }
  const tone = grade <= 'B' ? 'good' : grade <= 'C' ? 'mid' : 'weak'
  return { label: labels[grade] ?? grade, tone }
}
