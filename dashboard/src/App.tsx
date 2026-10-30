import { useEffect, useState } from 'react'
import { LiveProvider, useLive } from './hooks/live'
import { ROLES, type Role } from './api/types'
import { ConnectionIndicator, SimulatedBadge } from './components/common'
import { CasesBoard } from './screens/CasesBoard'
import { CaseView } from './screens/CaseView'
import { SystemsPanel } from './screens/SystemsPanel'
import { Handoffs } from './screens/Handoffs'
import { Audit } from './screens/Audit'
import { Control } from './screens/Control'
import { Coverage } from './screens/Coverage'
import { Ladder } from './screens/Ladder'
import { Judge } from './screens/Judge'
import { Arena } from './screens/Arena'
import { Phone } from './screens/Phone'
import { Simple } from './screens/Simple'
import { PhoneClient, recordMic } from './phone/PhoneClient'

const NAV = [
  ['cases', 'Cases'],
  ['systems', 'Systems'],
  ['handoffs', 'Handoffs'],
  ['coverage', 'Coverage'],
  ['judge', 'Judge Mode'],
  ['arena', 'Arena'],
  ['ladder', 'Ladder'],
  ['audit', 'Audit'],
  ['control', 'Control'],
] as const

export const parseHash = (h: string): { screen: string; arg: string | null } => {
  const [, screen = 'cases', arg] = h.replace(/^#/, '').split('/')
  return { screen: screen || 'cases', arg: arg ? decodeURIComponent(arg) : null }
}

function useHash() {
  const [h, setH] = useState(() => window.location.hash)
  useEffect(() => {
    const f = () => setH(window.location.hash)
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  return parseHash(h)
}

function Shell() {
  const { status, simTime, identity, setRole } = useLive()
  const { screen, arg } = useHash()
  const suffix = window.location.search.includes('mock=1') ? '?mock=1' : ''
  return (
    <div className="min-h-screen bg-slate-50 text-lg text-slate-900">
      <header className="flex flex-wrap items-center gap-4 border-b border-slate-300 bg-white px-6 py-3">
        <span className="text-2xl font-bold">Munim</span>
        <nav aria-label="Main" className="flex gap-1">
          {NAV.map(([id, label]) => (
            <a key={id} href={`${suffix}#/${id}`} aria-current={screen === id || (id === 'cases' && screen === 'case') ? 'page' : undefined}
              className={`rounded px-3 py-1 font-medium focus:outline-2 focus:outline-sky-600 ${screen === id || (id === 'cases' && screen === 'case') ? 'bg-slate-900 text-white' : 'hover:bg-slate-200'}`}>
              {label}
            </a>
          ))}
        </nav>
        <span className="ml-auto flex flex-wrap items-center gap-4">
          <SimulatedBadge />
          <span className="text-sm">Sim time: {simTime ?? 'unknown'}</span>
          <ConnectionIndicator status={status} />
          <label className="flex items-center gap-2 text-sm">
            Acting as
            <select className="rounded border border-slate-400 px-2 py-1" value={identity.role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLES.map((r) => <option key={r}>{r}</option>)}
            </select>
          </label>
        </span>
      </header>
      <main className="mx-auto max-w-[1600px] p-6">
        {screen === 'case' && arg ? <CaseView caseId={arg} /> : screen === 'systems' ? <SystemsPanel /> : screen === 'handoffs' ? <Handoffs />
          : screen === 'audit' ? <Audit /> : screen === 'coverage' ? <Coverage /> : screen === 'ladder' ? <Ladder /> : screen === 'judge' ? <Judge /> : screen === 'arena' ? <Arena /> : screen === 'phone' && arg ? <Phone token={arg} /> : screen === 'control' ? <Control /> : <CasesBoard />}
      </main>
    </div>
  )
}

export default function App() {
  const hash = parseHash(window.location.hash)
  if (hash.screen === 'simple' && hash.arg) {                 // the zero-literacy client has no dashboard chrome: icons and voice only
    return <LiveProvider><Simple token={hash.arg} /></LiveProvider>
  }
  if (location.pathname === '/phone') {
    return <PhoneClient token={new URLSearchParams(location.search).get('token') ?? ''} record={recordMic} />
  }
  return (
    <LiveProvider>
      <Shell />
    </LiveProvider>
  )
}
