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

function Shell() {
  const [tab, setTab] = useState<Tab>('picture')
  return (
    <>
      <header className="masthead">
        <div className="brand">
          HORUS
          <small>air domain awareness · GNSS-interference watch</small>
        </div>
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
        <div className="spacer" />
        <StatusPill />
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
