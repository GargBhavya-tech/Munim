import { AnimatePresence, motion } from 'motion/react'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import type { Api } from '@old/api/client'
import type { Claim, JudgeView, Reply, WhyResponse } from '@old/api/types'
import { useLive } from '@old/hooks/live'
import { dur, ease, useMotion } from './motion'
import { Button, Stamp, type Tone } from './ui'

/** The conversation, shared by the merchant phone, Judge Mode and the guard-off twin. A message goes through the production path (the judge session API);
 *  each reply then loads its PROOF: the sources of the sentences that were proven, and any action the policy refused with its rule id. Nothing is invented:
 *  a reply with no recorded render is shown as a fixed phrase. */

export interface Proof {
  claims: Claim[]
  fixed: boolean                                    // the reply is a fixed, approved phrase (no claims to source)
  refusals: { seq: number; action: string; rules: string[] }[]
  escalated: boolean
}
export interface Turn { id: number; who: 'you' | 'munim' | 'twin'; text: string; seq?: number | null; proof?: Proof | null; note?: string }

const LEVEL_TONE: Record<Claim['color'], Tone> = { green: 'ok', amber: 'warn', red: 'bad' }

export async function loadProof(api: Api, reply: Reply, sinceSeq: number): Promise<Proof> {
  const proof: Proof = { claims: [], fixed: false, refusals: [], escalated: false }
  if (!reply.case_id) return { ...proof, fixed: true }
  const [detail, page] = await Promise.all([api.caseDetail(reply.case_id), api.ledger(sinceSeq, 500)])
  const render = [...(detail.renders ?? [])].reverse().find((r) => r.sentences.map((s) => s.text).join(' ') === reply.text)
  if (render) {
    const ids = new Set(render.sentences.flatMap((s) => s.claim_ids))
    proof.claims = detail.claims.filter((c) => ids.has(c.id) && (c.verify_seq === undefined || c.verify_seq === render.verify_seq))
  }
  proof.fixed = proof.claims.length === 0
  for (const e of page.entries) {
    if (e.case_id !== reply.case_id) continue
    const p = e.payload as Record<string, unknown>
    if (e.kind === 'POLICY' && p.allow === false) proof.refusals.push({ seq: e.seq, action: String(p.action ?? 'an action'), rules: Array.isArray(p.rule_ids) ? (p.rule_ids as string[]) : [] })
    if (e.kind === 'ESCALATION') proof.escalated = true
  }
  return proof
}

/** The turns of one conversation and the way to add to it. `mark` is the latest ledger seq the page knows, so a reply's proof only reads what came after. */
export function useTurns() {
  const [turns, setTurns] = useState<Turn[]>([])
  const next = useRef(1)
  const add = useCallback((who: Turn['who'], text: string, extra: Partial<Turn> = {}) => {
    const id = next.current++
    setTurns((t) => [...t, { id, who, text, ...extra }])
    return id
  }, [])
  const patch = useCallback((id: number, p: Partial<Turn>) => setTurns((t) => t.map((x) => (x.id === id ? { ...x, ...p } : x))), [])
  const reset = useCallback(() => setTurns([]), [])
  return { turns, add, patch, reset }
}

export function useMark(fallback: number) {
  const { recent } = useLive()
  const ref = useRef(fallback)
  ref.current = Math.max(fallback, recent.length ? recent[recent.length - 1].seq : 0)
  return useCallback(() => ref.current, [])
}

/** One conversation: sends through the judge session (the production path) and attaches proof to each reply. */
export function useConversation(opts: { api: Api; view: JudgeView | null; onView: (v: JudgeView) => void; onReplies?: (r: Reply[]) => void }) {
  const { api, view, onView, onReplies } = opts
  const real = useTurns()
  const twin = useTurns()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [twinActions, setTwinActions] = useState<string[]>([])
  const [banner, setBanner] = useState<string | null>(null)
  const mark = useMark(view?.session.start_seq ?? 0)

  const attach = (sink: ReturnType<typeof useTurns>, who: Turn['who'], replies: Reply[], since: number) => {
    replies.forEach((r) => {
      const id = sink.add(who, r.text, { seq: r.seq })
      if (who === 'munim') loadProof(api, r, since).then((p) => sink.patch(id, { proof: p }), () => sink.patch(id, { proof: { claims: [], fixed: true, refusals: [], escalated: false } }))
    })
  }

  const say = async (text: string, opts: { twin?: boolean; language?: string } = {}) => {
    const withTwin = !!opts.twin
    const msg = text.trim()
    if (!view || !msg || busy) return
    setBusy(true)
    setError(null)
    const since = mark()
    real.add('you', msg)
    if (withTwin) twin.add('you', msg)
    try {
      if (withTwin) {
        const r = await api.twinMessage(view.session.merchant_id, msg, view.session.session_id)
        if (r.real.session && r.real.scoreboard) onView({ ...view, ...r.real } as JudgeView)
        setBanner(r.banner)
        attach(real, 'munim', r.real.replies, since)
        attach(twin, 'twin', r.twin.replies, since)
        if (r.twin.replies.length === 0) twin.add('twin', 'The twin gave no reply: the model could not be reached.')      // say so, never leave a blank panel
        setTwinActions(r.twin.actions.map((a) => `${a.action} ${a.status}`).concat(r.twin.nonexistent_actions_requested.map((n) => `${n}: does not exist`)))
        onReplies?.(r.real.replies)
      } else {
        const r = await api.judgeMessage(view.session.session_id, msg, opts.language)
        onView(r)
        attach(real, 'munim', r.replies ?? [], since)
        onReplies?.(r.replies ?? [])
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  /** What the server heard by voice, and the replies the engine wrote for it (they arrive with the transcript), with the same proof as a typed message. */
  const ingest = (text: string, replies: Reply[], since: number) => {
    real.add('you', text, { note: 'by voice' })
    attach(real, 'munim', replies, since)
  }
  return { turns: real.turns, twinTurns: twin.turns, say, ingest, busy, error, twinActions, banner, setBanner, reset: () => { real.reset(); twin.reset(); setTwinActions([]) } }
}

/** "Kyun nahi?": the rule, its source and why it refused, read from the ledger entry. */
export function WhyDrawer({ api, seq, onClose }: { api: Api; seq: number | null; onClose: () => void }) {
  const [why, setWhy] = useState<WhyResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const m = useMotion()
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    setWhy(null); setErr(null)
    if (seq == null) return
    api.why(seq).then(setWhy, (e: unknown) => setErr(e instanceof Error ? e.message : String(e)))
    closeRef.current?.focus()
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [api, seq, onClose])
  return (
    <AnimatePresence>
      {seq != null && (
        <motion.aside
          role="dialog" aria-modal="true" aria-label="Why this was refused"
          className="fixed inset-y-0 right-0 z-40 w-full max-w-md overflow-y-auto border-l border-line bg-surface p-6 text-fg"
          initial={{ x: m.reduce ? 0 : 40, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ opacity: 0 }}
          transition={{ duration: m.d(dur.base), ease: ease.out }}
        >
          <div className="flex items-start justify-between gap-4">
            <h2 className="text-h2">Kyun nahi?</h2>
            <button ref={closeRef} type="button" onClick={onClose} aria-label="Close" className="min-h-11 rounded-[4px] border border-line px-4 font-semibold hover:border-fg">Close</button>
          </div>
          {err && <p role="alert" className="mt-4 text-bad">Could not load the reason: {err}</p>}
          {!why && !err && <p className="mt-4 text-muted">Reading the ledger entry…</p>}
          {why && (
            <div className="mt-4 space-y-5">
              <p className="text-body">{why.summary}</p>
              <ul className="space-y-4">
                {why.rules.map((r) => (
                  <li key={r.rule_id} className="rule-t pt-3">
                    <Stamp>{r.rule_id}</Stamp>
                    <div className="text-h3 mt-3">{r.title}</div>
                    {r.reason && <p className="text-small mt-1">{r.reason}</p>}
                    <p className="text-small text-muted mt-1">Source: {r.source}</p>
                  </li>
                ))}
              </ul>
              <p className="text-label text-muted">ledger entry {why.seq} · {why.outcome}</p>
            </div>
          )}
        </motion.aside>
      )}
    </AnimatePresence>
  )
}

/** One message. Munim's words are serif; each proven sentence carries the stamp of its source; a refusal carries the rule and the way to ask why. */
export function Bubble({ turn, onWhy, align }: { turn: Turn; onWhy: (seq: number) => void; align?: 'left' | 'right' }) {
  const m = useMotion()
  const [open, setOpen] = useState<string | null>(null)
  const isYou = turn.who === 'you'
  const proof = turn.proof
  return (
    <motion.div
      className={`max-w-[92%] ${isYou ? 'ml-auto text-right' : align === 'right' ? 'ml-auto' : ''}`}
      initial={{ opacity: 0, y: m.y(10) }} animate={{ opacity: 1, y: 0 }} transition={{ duration: m.d(0.4), ease: ease.out }}
    >
      {turn.who === 'twin' && <div className="text-label text-bad mb-1">Twin · guards off</div>}
      {isYou
        ? <p className="inline-block bg-surface px-4 py-2 text-left text-body">{turn.text}{turn.note && <span className="text-label text-muted mt-1 block">{turn.note}</span>}</p>
        : <p className={`text-voice ${turn.who === 'twin' ? 'text-bad' : ''}`}>{turn.text}</p>}
      {!isYou && turn.who === 'munim' && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
          {!proof && <span className="text-small text-muted">checking proof…</span>}
          {proof?.fixed && proof.refusals.length === 0 && <Stamp tone="muted">fixed phrase</Stamp>}
          {proof?.claims.map((c, i) => (
            <button key={c.id + i} type="button" onClick={() => setOpen(open === c.id ? null : c.id)} aria-expanded={open === c.id} className="rounded-[4px] bg-transparent p-0">
              <Stamp tone={LEVEL_TONE[c.color]} delay={0.1 + i * 0.07}>{c.source}</Stamp>
            </button>
          ))}
          {proof?.refusals.map((r) => (
            <span key={r.seq} className="inline-flex items-center gap-2">
              <Stamp>refused · {r.rules.join(', ') || r.action}</Stamp>
              <Button className="min-h-9 px-3 text-small" onClick={() => onWhy(r.seq)}>Kyun nahi?</Button>
            </span>
          ))}
          {proof?.escalated && <Stamp tone="warn">handed to a person</Stamp>}
        </div>
      )}
      <AnimatePresence initial={false}>
        {open && proof && (() => {
          const c = proof.claims.find((x) => x.id === open)
          return c ? (
            <motion.div key={c.id} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="rule-t mt-3 pt-3 text-left text-small">
              <div className="text-label text-muted">{c.level.toLowerCase()} · {c.source}</div>
              <p className="mt-1">“{c.text}”</p>
              {c.snippet && <p className="mt-1 text-muted">Source says: {c.snippet}</p>}
              {c.reason && <p className="mt-1 text-muted">{c.reason}</p>}
            </motion.div>
          ) : null
        })()}
      </AnimatePresence>
    </motion.div>
  )
}

/** The message list. Presentational: the parent owns the conversation. */
export function ChatLog({ turns, onWhy, empty, align }: { turns: Turn[]; onWhy: (seq: number) => void; empty?: ReactNode; align?: 'left' | 'right' }) {
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { end.current?.scrollIntoView({ block: 'nearest' }) }, [turns.length, turns[turns.length - 1]?.proof])
  return (
    <div className="flex min-h-48 flex-col gap-5" aria-live="polite">
      {turns.length === 0 && empty}
      {turns.map((t) => <Bubble key={t.id} turn={t} onWhy={onWhy} align={align} />)}
      <div ref={end} />
    </div>
  )
}

export function Composer({ onSend, busy, label = 'Message', children }: { onSend: (text: string) => void; busy: boolean; label?: string; children?: ReactNode }) {
  const [text, setText] = useState('')
  return (
    <form className="mt-5 flex gap-2" onSubmit={(e) => { e.preventDefault(); const t = text; setText(''); onSend(t) }}>
      <input
        aria-label={label}
        className="min-h-11 flex-1 rounded-[4px] border border-line bg-surface px-3 text-fg placeholder:text-muted"
        value={text} onChange={(e) => setText(e.target.value)} disabled={busy} placeholder="Type as the merchant would…"
      />
      <Button type="submit" variant="primary" disabled={busy || !text.trim()}>{busy ? 'Waiting…' : 'Send'}</Button>
      {children}
    </form>
  )
}
