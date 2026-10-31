import { animate, motion, useMotionValue, useSpring, type HTMLMotionProps } from 'motion/react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { dur, ease, useMotion } from './motion'

/** The Ledger's building blocks (docs/FRONTEND_PLAN.md). Every motion here is driven by a state change and runs once; the only repeating one is the voice
 *  waveform, and it runs only while a voice is live. */

export type Surface = 'night' | 'paper'
export type Tone = 'stamp' | 'ink' | 'ok' | 'warn' | 'bad' | 'muted'
const TONE: Record<Tone, string> = { stamp: 'text-stamp', ink: 'text-ink', ok: 'text-ok', warn: 'text-warn', bad: 'text-bad', muted: 'text-muted' }

/** A surface re-skins everything inside it: night is the Back Office, paper is the Counter (the merchant's side). */
export function SurfaceBox({ surface, className = '', children }: { surface: Surface; className?: string; children: ReactNode }) {
  return <div data-surface={surface} className={`bg-bg text-fg ${className}`}>{children}</div>
}

/** THE motif. An ink stamp lands on a proven sentence (source id) or a refused action (rule id). Scale, a small rotation settle, opacity: once. */
export function Stamp({ children, tone = 'stamp', delay = 0, className = '' }: { children: ReactNode; tone?: Tone; delay?: number; className?: string }) {
  const m = useMotion()
  return (
    <motion.span
      className={`stamp ${TONE[tone]} ${className}`}
      initial={{ opacity: 0, scale: m.reduce ? 1 : 1.22, rotate: m.reduce ? -1.5 : -8 }}
      animate={{ opacity: 1, scale: 1, rotate: -1.5 }}
      transition={{ duration: m.d(0.34), delay: m.reduce ? 0 : delay, ease: ease.out }}
    >
      {children}
    </motion.span>
  )
}

/** A number that tweens to its new value (tabular numerals, so the width never shifts). */
export function Counter({ value, format = (n: number) => n.toLocaleString('en-IN'), className = '' }: { value: number; format?: (n: number) => string; className?: string }) {
  const m = useMotion()
  const mv = useMotionValue(value)
  const [shown, setShown] = useState(value)
  useEffect(() => {
    if (m.reduce) { setShown(value); mv.set(value); return }
    const stop = animate(mv, value, { duration: m.d(0.8), ease: ease.out })
    const off = mv.on('change', (v) => setShown(Math.round(v)))
    return () => { stop.stop(); off() }
  }, [value, m.reduce])   // eslint-disable-line react-hooks/exhaustive-deps
  return <span className={`text-num ${className}`}>{format(shown)}</span>
}

/** A mono label above a large number: how every metric is shown. A string value (for example "not measured yet") is shown as is, never as a zero. */
export function Readout({ label, value, unit, note, size = 'md', tone = 'ink' }: { label: string; value: number | string; unit?: string; note?: string; size?: 'md' | 'lg'; tone?: Tone }) {
  return (
    <div className="min-w-0">
      <div className="text-label text-muted">{label}</div>
      <div className={`${TONE[tone]} ${size === 'lg' ? 'text-h1' : 'text-h2'} leading-none mt-1`}>
        {typeof value === 'number' ? <Counter value={value} /> : <span className="text-h3 text-muted">{value}</span>}
        {unit && <span className="text-label text-muted ml-1.5">{unit}</span>}
      </div>
      {note && <div className="text-small text-muted mt-1">{note}</div>}
    </div>
  )
}

/** The voice. It moves ONLY while `live` (Munim speaking or the mic open), and not at all under reduced motion. */
export function Waveform({ live, bars = 28, className = '' }: { live: boolean; bars?: number; className?: string }) {
  const { reduce } = useMotion()
  return (
    <span aria-hidden="true" className={`inline-flex h-8 items-center gap-[3px] ${className}`}>
      {Array.from({ length: bars }, (_, i) => (
        <span
          key={i}
          className="block h-full w-[3px] origin-center rounded-full bg-current"
          style={{ transform: 'scaleY(0.25)', animation: live && !reduce ? `wave ${0.7 + (i % 5) * 0.11}s ease-in-out ${(i % 7) * 0.06}s infinite` : 'none' }}
        />
      ))}
    </span>
  )
}

/** One row of the ledger. A fresh entry turns in (opacity, small travel) and its chain rail draws from the previous entry. Old rows render still. */
export function LedgerEntry({ seq, kind, actor, summary, hash, fresh = false, first = false, tone = 'ink', children }: {
  seq: number; kind: string; actor?: string; summary: string; hash?: string; fresh?: boolean; first?: boolean; tone?: Tone; children?: ReactNode
}) {
  const m = useMotion()
  return (
    <motion.li
      className="rule-b relative grid grid-cols-[4rem_9.5rem_1fr_auto] items-baseline gap-x-4 py-3 pl-6 pr-2"
      initial={fresh ? { opacity: 0, y: m.y(14) } : false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: m.d(0.5), ease: ease.out }}
    >
      {!first && (
        <motion.span
          aria-hidden="true"
          className={`absolute left-[7px] top-0 h-3 w-px origin-top bg-current ${TONE[tone]}`}
          initial={fresh ? { scaleY: 0 } : false}
          animate={{ scaleY: 1 }}
          transition={{ duration: m.d(0.35), delay: m.reduce ? 0 : 0.1, ease: ease.out }}
        />
      )}
      <span aria-hidden="true" className={`absolute left-[4px] top-[1.15rem] h-[7px] w-[7px] bg-current ${TONE[tone]}`} />
      <span className="text-num text-muted">{seq}</span>
      <span className="text-label">{kind}</span>
      <span className="min-w-0 text-body">{summary}{actor && <span className="text-small text-muted"> · {actor}</span>}{children}</span>
      {hash && <span className="text-num text-small text-ink" title={hash}>{hash.slice(0, 12)}</span>}
    </motion.li>
  )
}

/** A block with an ink line that passes down it ONCE each time `run` changes (the chain verification). Does nothing under reduced motion. */
export function Sweep({ run, className = '', children }: { run: number; className?: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const { reduce } = useMotion()
  useEffect(() => {
    const el = ref.current
    if (!el || !run || reduce) return
    el.style.setProperty('--sweep-h', `${el.offsetHeight}px`)
    el.classList.remove('is-swept')
    void el.offsetWidth                       // restart the animation
    el.classList.add('is-swept')
    const t = setTimeout(() => el.classList.remove('is-swept'), dur.scene * 1000 + 120)
    return () => clearTimeout(t)
  }, [run, reduce])
  return <div ref={ref} className={`sweep ${className}`}>{children}</div>
}

/** The one primary action per screen wears the stamp colour; everything else is ghost. 44px tall, tactile press. */
export function Button({ variant = 'ghost', className = '', children, ...rest }: HTMLMotionProps<'button'> & { variant?: 'primary' | 'ghost' }) {
  const { reduce } = useMotion()
  const look = variant === 'primary' ? 'bg-stamp text-on-stamp border-stamp hover:brightness-110' : 'bg-transparent text-fg border-line hover:border-fg'
  return (
    <motion.button
      type="button"
      whileTap={reduce ? undefined : { scale: 0.97 }}
      transition={{ duration: dur.fast, ease: ease.out }}
      className={`inline-flex min-h-11 items-center justify-center gap-2 rounded-[4px] border px-5 font-semibold transition-colors duration-150 disabled:opacity-40 ${look} ${className}`}
      {...rest}
    >
      {children}
    </motion.button>
  )
}

export function Tag({ children, tone = 'muted' }: { children: ReactNode; tone?: Tone }) {
  return <span className={`text-label inline-block rounded-[2px] border border-line px-1.5 py-0.5 ${TONE[tone]}`}>{children}</span>
}

/** Enters once, when scrolled into view. */
export function Reveal({ children, delay = 0, className = '' }: { children: ReactNode; delay?: number; className?: string }) {
  const m = useMotion()
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: m.y(16) }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '-8% 0px' }}
      transition={{ duration: m.d(0.6), delay: m.reduce ? 0 : delay, ease: ease.out }}
    >
      {children}
    </motion.div>
  )
}

/** A control that leans a little toward the pointer. Off for touch and for reduced motion. */
export function Magnetic({ children, strength = 0.3, className = '' }: { children: ReactNode; strength?: number; className?: string }) {
  const { reduce } = useMotion()
  const x = useSpring(useMotionValue(0), { stiffness: 220, damping: 18 })
  const y = useSpring(useMotionValue(0), { stiffness: 220, damping: 18 })
  const ref = useRef<HTMLDivElement>(null)
  const fine = typeof window !== 'undefined' && window.matchMedia('(pointer: fine)').matches
  if (reduce || !fine) return <div className={className}>{children}</div>
  return (
    <motion.div
      ref={ref}
      className={className}
      style={{ x, y }}
      onPointerMove={(e) => {
        const r = ref.current?.getBoundingClientRect()
        if (!r) return
        x.set((e.clientX - (r.left + r.width / 2)) * strength)
        y.set((e.clientY - (r.top + r.height / 2)) * strength)
      }}
      onPointerLeave={() => { x.set(0); y.set(0) }}
    >
      {children}
    </motion.div>
  )
}
