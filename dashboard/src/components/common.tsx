import type { ReactNode } from 'react'
import type { ConnStatus } from '../lib/ws'

export const SimulatedBadge = () => (
  <span className="rounded border-2 border-amber-600 bg-amber-100 px-2 py-0.5 text-sm font-bold tracking-wide text-amber-900">SIMULATED DATA</span>
)

const STATUS_TEXT: Record<ConnStatus, string> = {
  connecting: 'Connecting',
  open: 'Live',
  reconnecting: 'Reconnecting',
  closed: 'Disconnected',
  mock: 'Mock data (no network)',
}
const STATUS_COLOR: Record<ConnStatus, string> = {
  connecting: 'bg-amber-500',
  open: 'bg-emerald-500',
  reconnecting: 'bg-amber-500',
  closed: 'bg-red-600',
  mock: 'bg-sky-500',
}

export const ConnectionIndicator = ({ status }: { status: ConnStatus }) => (
  <span role="status" aria-live="polite" className="inline-flex items-center gap-2 text-sm" data-testid="conn">
    <span aria-hidden className={`inline-block h-3 w-3 rounded-full ${STATUS_COLOR[status]}`} />
    {STATUS_TEXT[status]}
  </span>
)

export const Panel = ({ title, children, className = '' }: { title?: ReactNode; children: ReactNode; className?: string }) => (
  <section className={`rounded-lg border border-slate-300 bg-white p-4 shadow-sm ${className}`}>
    {title && <h2 className="mb-2 text-xl font-semibold">{title}</h2>}
    {children}
  </section>
)

export const ErrorNote = ({ error }: { error: string | null }) =>
  error ? (
    <p role="alert" className="rounded border border-red-300 bg-red-50 p-2 text-red-800">
      {error}
    </p>
  ) : null

export const Loading = () => <p className="text-slate-600">Loading...</p>

export const btn = 'rounded border border-slate-400 bg-white px-3 py-1.5 font-medium hover:bg-slate-100 focus:outline-2 focus:outline-offset-2 focus:outline-sky-600 disabled:cursor-not-allowed disabled:opacity-50'
export const btnPrimary = 'rounded bg-slate-900 px-3 py-1.5 font-medium text-white hover:bg-slate-700 focus:outline-2 focus:outline-offset-2 focus:outline-sky-600 disabled:cursor-not-allowed disabled:opacity-50'
export const btnDanger = 'rounded bg-red-700 px-3 py-1.5 font-medium text-white hover:bg-red-800 focus:outline-2 focus:outline-offset-2 focus:outline-sky-600 disabled:cursor-not-allowed disabled:opacity-50'
export const inputCls = 'rounded border border-slate-400 px-2 py-1.5 focus:outline-2 focus:outline-sky-600'

export const Json = ({ value }: { value: unknown }) => (
  <pre className="max-h-64 overflow-auto rounded bg-slate-100 p-2 text-sm whitespace-pre-wrap">{JSON.stringify(value, null, 2)}</pre>
)
