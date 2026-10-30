import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { pickApi, type Api } from '../api/client'
import { MOCK_SIM_TIME } from '../api/mock'
import { ROLES, type Identity, type LedgerEntry, type Role } from '../api/types'
import { ReconnectingSocket, wsUrl, type ConnStatus, type SocketLike } from '../lib/ws'

type Listener = (e: LedgerEntry) => void

interface Live {
  api: Api
  mock: boolean
  status: ConnStatus
  simTime: string | null
  setSimTime: (iso: string) => void
  recent: LedgerEntry[]
  onLedger: (fn: Listener) => () => void
  identity: Identity
  setRole: (r: Role) => void
}

const Ctx = createContext<Live | null>(null)
export const useLive = (): Live => {
  const v = useContext(Ctx)
  if (!v) throw new Error('LiveProvider missing')
  return v
}

const RECENT_CAP = 300

export function LiveProvider({ children, api, mock, socketFactory }: { children: ReactNode; api?: Api; mock?: boolean; socketFactory?: (u: string) => SocketLike }) {
  const isMock = mock ?? new URLSearchParams(window.location.search).get('mock') === '1'
  const theApi = useMemo(() => api ?? pickApi(), [api])
  const [status, setStatus] = useState<ConnStatus>(isMock ? 'mock' : 'connecting')
  const [simTime, setSimTime] = useState<string | null>(isMock ? MOCK_SIM_TIME : null)
  const [recent, setRecent] = useState<LedgerEntry[]>([])
  const listeners = useRef(new Set<Listener>())
  const [role, setRole] = useState<Role>('support_executive')

  useEffect(() => {
    if (isMock) return
    const sock = new ReconnectingSocket({
      url: wsUrl(),
      factory: socketFactory ?? ((u) => new WebSocket(u) as unknown as SocketLike),
      onStatus: (s) => setStatus(s),
      onMessage: (m) => {
        if (m.type === 'hello') setSimTime(m.sim_time)
        else {
          setSimTime((cur) => (cur && Date.parse(cur) > Date.parse(m.entry.ts_sim) ? cur : m.entry.ts_sim))
          setRecent((r) => [...r.slice(-(RECENT_CAP - 1)), m.entry])
          listeners.current.forEach((f) => f(m.entry))
        }
      },
    })
    sock.start()
    return () => sock.stop()
  }, [isMock, socketFactory])

  // Initial sim time when the socket has not said hello yet (single request, not a poll).
  useEffect(() => {
    if (isMock) return
    theApi.health().then((h) => setSimTime((c) => c ?? h.sim_time)).catch(() => {})
  }, [theApi, isMock])

  const onLedger = useCallback((fn: Listener) => {
    listeners.current.add(fn)
    return () => void listeners.current.delete(fn)
  }, [])

  // Identity is only a label sent with writes. The server decides what it may do.
  const identity = useMemo<Identity>(() => ({ principal_id: `demo-${role}`, role }), [role])
  const value = useMemo(
    () => ({ api: theApi, mock: isMock, status, simTime, setSimTime: (iso: string) => setSimTime(iso), recent, onLedger, identity, setRole }),
    [theApi, isMock, status, simTime, recent, onLedger, identity],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export { ROLES }

/** Load a resource; refetch (debounced) when a ledger event passes `relevant`. No timers poll the server. */
export function useResource<T>(load: () => Promise<T>, deps: unknown[], relevant?: (e: LedgerEntry) => boolean, debounceMs = 200) {
  const { onLedger } = useLive()
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const loadRef = useRef(load)
  loadRef.current = load
  const seq = useRef(0)
  const run = useCallback(() => {
    const my = ++seq.current
    loadRef.current().then(
      (d) => {
        if (my === seq.current) {
          setData(d)
          setError(null)
        }
      },
      (e: unknown) => {
        if (my === seq.current) setError(e instanceof Error ? e.message : String(e))
      },
    )
  }, [])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(run, deps)
  const relRef = useRef(relevant)
  relRef.current = relevant
  useEffect(() => {
    if (!relevant) return
    let t: ReturnType<typeof setTimeout> | undefined
    const off = onLedger((e) => {
      if (relRef.current && !relRef.current(e)) return
      clearTimeout(t)
      t = setTimeout(run, debounceMs)
    })
    return () => {
      off()
      clearTimeout(t)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onLedger, run, debounceMs, relevant !== undefined])
  return { data, error, reload: run, setData }
}
