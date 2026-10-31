import type { ReactNode } from 'react'
import { ApiError } from '@old/api/client'
import { Stamp } from './ui'

/** Small shared pieces for the back-office screens. Nothing here decides anything: refusals are the server's own words. */

export const field = 'min-h-11 rounded-[4px] border border-line bg-surface px-3 text-fg placeholder:text-muted'
export const fieldSm = 'min-h-9 rounded-[2px] border border-line bg-surface px-2 text-fg'

export function Page({ eyebrow, title, intro, children }: { eyebrow: string; title: string; intro?: ReactNode; children: ReactNode }) {
  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <div className="text-label text-muted">{eyebrow}</div>
      <h1 className="text-h1 mt-1">{title}</h1>
      {intro && <p className="text-body text-muted mt-4 max-w-[62ch]">{intro}</p>}
      <div className="mt-8">{children}</div>
    </div>
  )
}

export function Section({ title, note, children }: { title: string; note?: ReactNode; children: ReactNode }) {
  return (
    <section className="rule-t mt-10 pt-6" aria-label={title}>
      <h2 className="text-h3">{title}</h2>
      {note && <p className="text-small text-muted mt-1 max-w-[60ch]">{note}</p>}
      <div className="mt-4">{children}</div>
    </section>
  )
}

export function Json({ value }: { value: unknown }) {
  return <pre className="text-num text-small max-h-64 overflow-auto whitespace-pre-wrap border border-line bg-bg p-3">{JSON.stringify(value, null, 2)}</pre>
}

export function Note({ error }: { error: string | null }) {
  return error ? <p role="alert" className="text-bad text-small mt-3">{error}</p> : null
}

/** The server's refusal, verbatim: rule id and reason. */
export function Refusal({ error }: { error: unknown }) {
  if (!error) return null
  if (error instanceof ApiError) {
    const b = error.refusal.body
    return (
      <div role="alert" className="mt-3 flex flex-wrap items-baseline gap-3 border border-bad p-3">
        <Stamp tone="bad">refused · {error.refusal.httpStatus}{b.rule_id ? ` · ${b.rule_id}` : ''}</Stamp>
        <span className="text-small">{String(b.reason ?? (typeof b.detail === 'string' ? b.detail : '') ?? '')}</span>
      </div>
    )
  }
  return <p role="alert" className="text-bad text-small mt-3">{error instanceof Error ? error.message : String(error)}</p>
}

export const loading = <p className="text-muted">Loading…</p>
