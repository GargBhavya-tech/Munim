import { AnimatePresence, motion } from 'motion/react'
import { useEffect, useState, type ReactNode } from 'react'
import { ROLES, useLive, useResource } from '@old/hooks/live'
import type { Role } from '@old/api/types'
import type { ConnStatus } from '@old/lib/ws'
import { ScreenGuide } from './guide'
import { dur, ease, useMotion } from './motion'
import { Stamp, SurfaceBox, type Surface } from './ui'

/** The frame every screen sits in: wordmark, nav, the always-visible SIMULATED stamp, the sim clock, the connection light, the kill-switch state and the role.
 *  Nothing here is decorative: every item shows a real state or goes somewhere real. */

export interface Route {
  label: string
  group: string                         // the nav heading it sits under
  surface: Surface                      // night = Back Office, paper = the Counter
  render: (arg: string | null) => ReactNode
}

export const parseHash = (h: string): { screen: string; arg: string | null } => {
  const [, screen = '', arg] = h.replace(/^#/, '').split('/')
  return { screen, arg: arg ? decodeURIComponent(arg) : null }
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

const CONN: Record<ConnStatus, { text: string; cls: string }> = {
  open: { text: 'Live', cls: 'bg-ok' },
  connecting: { text: 'Connecting', cls: 'bg-warn' },
  reconnecting: { text: 'Reconnecting', cls: 'bg-warn' },
  closed: { text: 'Offline', cls: 'bg-bad' },
  mock: { text: 'Recorded', cls: 'bg-warn' },
}

/** The server's simulated time, shown the way a person reads it. The time is the server's; this only formats it. */
export function fmtSim(iso: string | null): string {
  if (!iso) return 'sim time unknown'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) + ' IST'
}

/** What each role may do, from the permission matrix in core/engine/roles.py. Every role can read everything; this lists the writes, and who can ask the ledger. */
const ROLE_NOTE: Record<string, string> = {
  support_executive: 'Can follow up, book, raise tickets, send messages, start replacements and refunds, waive a fee, and undo these. Cannot clear a risk hold or approve plan changes.',
  risk_reviewer: 'Can clear a risk hold, raise tickets, send messages, and undo ticket, refund and closure requests. Cannot change fees, plans or devices.',
  sales_lead: 'Can waive fees, change plans, order devices, send partner links and follow up, and undo those. Cannot clear a risk hold.',
  ops_admin: 'Can do every write except clearing a risk hold (reserved for the risk reviewer), and approves policy rules. Still bound by every other rule.',
  auditor: 'Reads everything and can question the ledger. Cannot change anything.',
}

function StatusBar() {
  const { status, simTime, identity, setRole, mock, api } = useLive()
  const control = useResource(() => api.control(), [api], (e) => e.kind === 'HUMAN_ACTION')
  const c = CONN[status]
  const killed = control.data?.kill_switch
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-small">
      <Stamp>SIMULATED DATA</Stamp>
      <span className="text-num text-muted">{fmtSim(simTime)}</span>
      <span className="inline-flex items-center gap-2" role="status">
        <span aria-hidden="true" className={`inline-block h-2 w-2 rounded-full ${c.cls}`} />
        {c.text}
      </span>
      {killed !== undefined && (killed ? <Stamp tone="bad">KILL SWITCH ON</Stamp> : <span className="text-muted">Kill switch off</span>)}
      <label className="inline-flex items-center gap-2 text-muted">
        Acting as
        <select
          className="min-h-9 rounded-[2px] border border-line bg-surface px-2 text-fg"
          value={identity.role}
          onChange={(e) => setRole(e.target.value as Role)}
        >
          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
      </label>
      {mock && <Stamp tone="warn">RECORDED DATA, NOT LIVE</Stamp>}
      <p role="status" className="basis-full text-muted">As <b className="text-fg">{identity.role}</b>: {ROLE_NOTE[identity.role] ?? 'Reads everything.'}</p>
    </div>
  )
}

export function Shell({ routes, fallback }: { routes: Record<string, Route>; fallback: string }) {
  const { screen, arg } = useHash()
  const m = useMotion()
  const key = routes[screen] ? screen : fallback
  const route = routes[key]
  const suffix = window.location.search
  const groups = Object.entries(routes).reduce<[string, [string, Route][]][]>((acc, entry) => {
    const g = acc.find(([name]) => name === entry[1].group)
    if (g) g[1].push(entry)
    else acc.push([entry[1].group, [entry]])
    return acc
  }, [])
  return (
    <SurfaceBox surface="night" className="min-h-screen">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:bg-surface focus:px-3 focus:py-2">Skip to content</a>
      <header className="rule-b px-6 py-4">
        <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
          <a href={`${suffix}#/${fallback}`} className="text-h2 leading-none no-underline" aria-label="Munim home">Munim</a>
          <nav aria-label="Main" className="flex flex-wrap gap-x-6 gap-y-3">
            {groups.map(([group, items]) => (
              <div key={group} role="group" aria-label={group}>
                <div className="text-label px-3 text-[0.7rem] text-muted">{group}</div>
                <div className="flex flex-wrap gap-1">
                  {items.map(([id, r]) => (
                    <a
                      key={id}
                      href={`${suffix}#/${id}`}
                      aria-current={key === id ? 'page' : undefined}
                      className={`inline-flex min-h-10 items-center rounded-[2px] px-3 font-medium no-underline transition-colors duration-150 ${key === id ? 'bg-fg text-bg' : 'text-muted hover:text-fg'}`}
                    >
                      {r.label}
                    </a>
                  ))}
                </div>
              </div>
            ))}
            <div>
              <div className="text-label px-3 text-[0.7rem] text-muted">Elsewhere</div>
              <a href="http://localhost:5173/" className="inline-flex min-h-10 items-center px-3 text-muted no-underline hover:text-fg">Original dashboard ↗</a>
            </div>
          </nav>
        </div>
        <div className="mt-3"><StatusBar /></div>
      </header>
      <main id="main">
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={key}
            initial={{ opacity: 0, y: m.y(8) }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: m.d(dur.base), ease: ease.inOut }}
          >
            <SurfaceBox surface={route.surface} className="min-h-[calc(100vh-9rem)]">
              <ScreenGuide id={key} />
              {route.render(arg)}
            </SurfaceBox>
          </motion.div>
        </AnimatePresence>
      </main>
    </SurfaceBox>
  )
}
