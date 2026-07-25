import { useState } from 'react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { api } from './api'
import AirPicture from './views/AirPicture'
import Incidents from './views/Incidents'
import HowItWorks from './views/HowItWorks'

const client = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
})

type Tab = 'picture' | 'incidents' | 'how'

const TABS: { id: Tab; label: string }[] = [
  { id: 'picture', label: 'Air Picture' },
  { id: 'incidents', label: 'Incidents' },
  { id: 'how', label: 'How it works' },
]

function StatusPill() {
  const { data, isError } = useQuery({ queryKey: ['health'], queryFn: api.health })
  if (isError) return <span className="status-pill">API offline</span>
  if (!data) return <span className="status-pill">…</span>
  return <span className="status-pill ok">● API v{data.version}</span>
}

/** How old the newest report is. Shown always, not only when stale: a viewer should never
 *  have to assume the sky on screen is current — the age says so explicitly. */
function Freshness() {
  const { data } = useQuery({
    queryKey: ['stats'],
    queryFn: api.stats,
    refetchInterval: 30_000,
  })
  if (!data) return null
  // A baked demo is honestly a snapshot, not a dead live feed — say so plainly rather than
  // showing a staleness warning that implies something is broken.
  if (data.demo_mode) {
    return (
      <span className="status-pill snapshot" title="Baked synthetic snapshot — run locally for the live lane">
        ◆ demo snapshot
      </span>
    )
  }
  if (data.data_age_seconds == null) return null
  const mins = data.data_age_seconds / 60
  const stale = mins > 5
  const label =
    mins < 1 ? 'live' : mins < 90 ? `${mins.toFixed(0)} min old` : `${(mins / 60).toFixed(1)} h old`
  return (
    <span className={`status-pill${stale ? '' : ' ok'}`} title={`newest report ${data.newest_report}`}>
      {stale ? '◐' : '●'} data {label}
    </span>
  )
}

function Shell() {
  const [tab, setTab] = useState<Tab>('picture')
  return (
    <>
      <header className="masthead">
        <div className="brand">
          HORUS
          <small>air domain awareness · GNSS-interference watch</small>
        </div>
        <Freshness />
        <StatusPill />
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`tab${tab === t.id ? ' active' : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {tab === 'picture' && <AirPicture />}
        {tab === 'incidents' && <Incidents />}
        {tab === 'how' && <HowItWorks />}
      </main>
      <footer className="foot">
        Public ADS-B broadcasts only · incidents are human-review decision support, never
        automated verdicts · watch rings are illustrative, not authoritative airspace
      </footer>
    </>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={client}>
      <Shell />
    </QueryClientProvider>
  )
}
