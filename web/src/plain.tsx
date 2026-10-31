import { useId, type ReactNode } from 'react'
import { explain, key } from './glossary'

/** The plain-language layer: a word with a meaning on hover, focus or tap, and the small formatters every screen shares. No motion here beyond a 140 ms fade. */

/** A term with a dotted underline. Hover, keyboard focus or a tap opens a short plain explanation; Escape closes it. A term with no entry renders as plain text. */
export function Term({ term, children }: { term: string; children?: ReactNode }) {
  const id = useId()
  const meaning = explain(term)
  if (!meaning) return <>{children ?? term}</>
  return (
    <span className="term group relative inline-block">
      <span
        tabIndex={0}
        aria-describedby={id}
        className="cursor-help border-b border-dotted border-current outline-offset-2"
        onKeyDown={(e) => { if (e.key === 'Escape') (e.currentTarget as HTMLElement).blur() }}
      >
        {children ?? term}
      </span>
      <span
        id={id}
        role="tooltip"
        data-term={key(term)}
        className="pointer-events-none invisible absolute left-0 top-full z-30 mt-2 w-72 max-w-[80vw] border border-line bg-surface px-3 py-2 text-small font-normal normal-case tracking-normal text-fg opacity-0 transition-opacity duration-150 group-focus-within:visible group-focus-within:opacity-100 group-hover:visible group-hover:opacity-100"
      >
        {meaning}
      </span>
    </span>
  )
}

/** Integer paise shown as rupees. Display formatting only: the amount is the server's, never computed here. */
export function rupees(paise: unknown): string {
  return typeof paise === 'number' ? `₹${(paise / 100).toLocaleString('en-IN', { maximumFractionDigits: 2 })}` : 'unknown'
}

export const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
export const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** A status with its meaning, never colour alone: a mark (tick, cross, dots) and a word go with the colour. */
export type Mood = 'ok' | 'bad' | 'wait' | 'none'
export const MARK: Record<Mood, string> = { ok: '✓', bad: '✕', wait: '…', none: '–' }
export const MOOD_CLASS: Record<Mood, string> = { ok: 'text-ok', bad: 'text-bad', wait: 'text-warn', none: 'text-muted' }

export function Mark({ mood, children }: { mood: Mood; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-2 font-semibold ${MOOD_CLASS[mood]}`}>
      <span aria-hidden="true" className="text-num inline-flex h-6 w-6 items-center justify-center border border-current text-small leading-none">{MARK[mood]}</span>
      <span>{children}</span>
    </span>
  )
}
