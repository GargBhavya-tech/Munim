import { useEffect, useState } from 'react'
import { LiveProvider } from '@old/hooks/live'
import { Arena } from './screens/Arena'
import { Control } from './screens/Control'
import { Coverage } from './screens/Coverage'
import { Design } from './screens/Design'
import { Floor } from './screens/Floor'
import { Handoffs } from './screens/Handoffs'
import { Judge } from './screens/Judge'
import { Ladder } from './screens/Ladder'
import { Leads } from './screens/Leads'
import { Opening } from './screens/Opening'
import { Ops } from './screens/Ops'
import { Phone } from './screens/Phone'
import { Proof } from './screens/Proof'
import { Desk } from './screens/Desk'
import { Sales } from './screens/Sales'
import { Simple } from './screens/Simple'
import { Systems } from './screens/Systems'
import { parseHash, Shell, type Route } from './shell'

/** The judge-facing app (docs/FRONTEND_PLAN.md). A screen that is not built yet is not listed, so no navigation item goes nowhere; the original dashboard link in
 *  the header covers everything not rebuilt (handoffs, systems, arena, metrics). */
const ROUTES: Record<string, Route> = {
  judge: { label: 'Judge Mode', group: 'Try it', surface: 'night', render: (arg) => <Judge personaArg={arg} /> },
  phone: { label: 'Merchant phone', group: 'Try it', surface: 'night', render: (arg) => <Phone arg={arg} /> },
  arena: { label: 'Arena', group: 'Try it', surface: 'night', render: () => <Arena /> },
  floor: { label: 'Case Floor', group: 'Watch it work', surface: 'night', render: () => <Floor /> },
  desk: { label: 'Sales desk', group: 'Watch it work', surface: 'night', render: () => <Desk /> },
  sales: { label: 'Sales', group: 'Watch it work', surface: 'night', render: () => <Sales /> },
  leads: { label: 'New merchants', group: 'Watch it work', surface: 'night', render: () => <Leads /> },
  ops: { label: 'Service ops', group: 'Watch it work', surface: 'night', render: () => <Ops /> },
  systems: { label: 'Systems', group: 'Watch it work', surface: 'night', render: () => <Systems /> },
  handoffs: { label: 'Handoffs', group: 'Watch it work', surface: 'night', render: () => <Handoffs /> },
  proof: { label: 'Proof Ledger', group: 'Check the proof', surface: 'night', render: () => <Proof /> },
  coverage: { label: 'Coverage', group: 'Check the proof', surface: 'night', render: () => <Coverage /> },
  ladder: { label: 'Ladder', group: 'Check the proof', surface: 'night', render: () => <Ladder /> },
  control: { label: 'Control', group: 'Operate', surface: 'night', render: () => <Control /> },
  design: { label: 'Design system', group: 'Operate', surface: 'night', render: () => <Design /> },
}

const seen = () => {
  try { return sessionStorage.getItem('munim-opened') === '1' || new URLSearchParams(window.location.search).has('skip') } catch { return true }
}

/** Links from the original dashboard keep working: #/cases and #/case/<id> are the Case Floor, #/audit is the Proof Ledger. */
const ALIAS: Record<string, string> = { cases: 'floor', case: 'floor', audit: 'proof' }

export default function App() {
  const [opened, setOpened] = useState(seen)
  const [hash, setHash] = useState(() => window.location.hash)
  useEffect(() => {
    const f = () => setHash(window.location.hash)
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  const { screen, arg } = parseHash(hash)
  useEffect(() => { if (ALIAS[screen]) window.location.replace(`${window.location.search}#/${ALIAS[screen]}`) }, [screen])
  if (screen === 'simple' && arg) return <LiveProvider><Simple token={arg} /></LiveProvider>       // the zero-literacy client has no chrome: icons and voice only
  return (
    <LiveProvider>
      {!opened && <Opening onDone={() => { try { sessionStorage.setItem('munim-opened', '1') } catch { /* storage can be blocked: the opening then plays again, which is harmless */ } setOpened(true) }} />}
      <Shell routes={ROUTES} fallback="floor" />
    </LiveProvider>
  )
}
