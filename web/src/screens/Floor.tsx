import { motion } from 'motion/react'
import { useRef, useState } from 'react'
import { useLive, useResource } from '@old/hooks/live'
import { groupByState } from '@old/lib/board'
import { undoState, clockText } from '@old/lib/undo'
import { backTranslations, speak } from '@old/lib/conversation'
import { Json } from '../parts'
import type { Case, UndoResult } from '@old/api/types'
import { ease, useMotion } from '../motion'
import { Button, Stamp, Tag, type Tone } from '../ui'

/** The Case Floor: every case Munim is carrying, grouped by where it stands. Rows turn in the moment a case opens or changes. A case a person has taken shows it.
 *  Open a case to see what was said, what was done, and to Undo an action while its window is open. */

const STATE_TONE: Record<string, Tone> = { ESCALATED: 'stamp', BLOCKED: 'bad', RESOLVED: 'ok', WAITING: 'warn', PARKED: 'muted' }
const nice = (s: string) => s.replace(/_/g, ' ').toLowerCase()

function CaseBody({ c }: { c: Case }) {
  const { api, identity, simTime } = useLive()
  const detail = useResource(() => api.caseDetail(c.case_id), [api, c.case_id], (e) => e.case_id === c.case_id)
  const [undone, setUndone] = useState<Record<string, UndoResult | string>>({})
  const [english, setEnglish] = useState(false)
  const d = detail.data
  const back = backTranslations(d?.renders ?? [])
  if (detail.error) return <p role="alert" className="p-4 text-bad">Could not load this case: {detail.error}</p>
  if (!d) return <p className="p-4 text-muted">Opening the case…</p>
  return (
    <div className="grid gap-8 p-5 md:grid-cols-2">
      <div>
        <h3 className="text-label text-muted">What was said</h3>
        <label className="text-small mt-2 flex items-center gap-2 text-muted"><input type="checkbox" checked={english} onChange={(e) => setEnglish(e.target.checked)} /> Show Munim's replies as the gate's English back-translation</label>
        {d.messages.length === 0 && <p className="text-small text-muted mt-2">Nothing has been said in this case yet.</p>}
        <ul className="mt-2 space-y-3">
          {d.messages.map((m, i) => (
            <li key={i} className={m.direction === 'IN' ? '' : 'text-voice'}>
              <span className="text-label text-muted mr-2">{m.direction === 'IN' ? 'merchant' : 'munim'} · {english && m.direction === 'OUT' && back(m.text) ? 'en (back-translation)' : m.language} · {m.ts_sim}</span>
              {english && m.direction === 'OUT' && back(m.text) ? back(m.text) : m.text}
              <button type="button" onClick={() => speak(m.text, m.language)} aria-label={`Play message ${i + 1}`} className="text-label ml-3 text-ink underline-offset-4 hover:underline">play</button>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <h3 className="text-label text-muted">What was done</h3>
        {d.actions.length === 0 && <p className="text-small text-muted mt-2">No action has run in this case.</p>}
        <ul className="mt-2 space-y-3">
          {d.actions.map((a) => {
            const u = undoState(a, simTime)
            const res = undone[a.action_id]
            return (
              <li key={a.action_id} className="flex flex-wrap items-center gap-3">
                <span className="text-num">{a.name}</span>
                <Tag tone={a.status === 'OK' ? 'ok' : 'muted'}>{a.status}</Tag>
                <span className="text-label text-muted">{a.cls}</span>
                {u.enabled && !res && <Button className="min-h-9 px-3 text-small" onClick={() => api.undo(a.action_id, identity).then((r) => { setUndone((x) => ({ ...x, [a.action_id]: r })); detail.reload() }, (e: unknown) => setUndone((x) => ({ ...x, [a.action_id]: e instanceof Error ? e.message : String(e) })))}>{u.label}</Button>}
                {!u.enabled && a.compensable_until && !res && <span className="text-small text-muted">{u.label}</span>}
                {res && (typeof res === 'string' ? <span role="alert" className="text-bad text-small">{res}</span> : <Stamp tone={res.status === 'OK' ? 'ok' : 'bad'}>undo · {res.status}{res.error ? ` · ${res.error}` : ''}</Stamp>)}
              </li>
            )
          })}
        </ul>
        {d.case.resume_note && <p className="text-small text-muted mt-5">Note: {d.case.resume_note}</p>}
        {(d.receipts?.length ? d.receipts.map((r) => `${r.label} · ${r.cancel}`) : d.actions.filter((a) => a.undoable && a.compensable_until).map((a) => `${a.name.replace(/_/g, ' ')} done · cancel until ${clockText(a.compensable_until!)}`)).map((t, i) => <p key={i} className="text-small mt-4 border border-ok px-3 py-2">{t}</p>)}
      </div>
      <div className="md:col-span-2">
        <h3 className="text-label text-muted">What the grounding gate decided</h3>
        {d.claims.length === 0 && <p className="text-small text-muted mt-2">No claim checks recorded yet.</p>}
        <ul className="mt-2 space-y-2">
          {d.claims.map((c) => (
            <li key={`${c.verify_seq ?? ''}-${c.id}`}>
              <details>
                <summary className="cursor-pointer"><span className={`text-label mr-2 ${c.color === 'green' ? 'text-ok' : c.color === 'amber' ? 'text-warn' : 'text-bad'}`}>{c.color === 'green' ? 'Sent' : c.color === 'amber' ? 'Removed before sending' : 'Blocked'} · {c.level}</span>{c.color === 'green' ? c.text : <span>would have said “{c.text}”</span>}</summary>
                <dl className="text-small mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
                  <dt className="text-muted">source</dt><dd className="break-all">{c.source}</dd>
                  {c.reason && <><dt className="text-muted">reason</dt><dd>{c.reason}</dd></>}
                  {c.rule_id && <><dt className="text-muted">rule</dt><dd>{c.rule_id}</dd></>}
                  {c.nli_label && <><dt className="text-muted">entailment</dt><dd>{c.nli_label}{c.nli_score != null ? ` (${c.nli_score.toFixed(2)})` : ''}</dd></>}
                  {c.corroborated_by && c.corroborated_by.length > 0 && <><dt className="text-muted">also backed by</dt><dd className="break-all">{c.corroborated_by.join(', ')}</dd></>}
                  {c.snippet && <><dt className="text-muted">evidence</dt><dd>{c.snippet}</dd></>}
                  {c.verify_seq != null && <><dt className="text-muted">ledger</dt><dd>VERIFY #{c.verify_seq}</dd></>}
                </dl>
              </details>
            </li>
          ))}
        </ul>
        <h3 className="text-label mt-8 text-muted">Decision timeline (ledger)</h3>
        <ol className="mt-2 space-y-1">
          {d.timeline.map((e) => (
            <li key={e.seq}>
              <details>
                <summary className="cursor-pointer"><span className="text-num text-muted">#{e.seq}</span> <b>{e.kind}</b> <span className="text-muted">{e.actor}{e.model_id ? ` (${e.model_id})` : ''}</span></summary>
                <Json value={e.payload} />
                <p className="text-num text-small break-all text-muted">policy {e.policy_hash ?? '-'} · hash {e.hash}</p>
              </details>
            </li>
          ))}
        </ol>
      </div>
    </div>
  )
}

export function Floor() {
  const { api } = useLive()
  const m = useMotion()
  const cases = useResource(() => api.cases(), [api], (e) => e.kind === 'STATE' || e.kind === 'ESCALATION')
  const [open, setOpen] = useState<string | null>(null)
  const known = useRef<Set<string> | null>(null)
  const list = cases.data?.cases ?? []
  if (known.current === null && cases.data) known.current = new Set(list.map((c) => c.case_id))     // cases present at first load are still; later ones turn in
  const columns = groupByState(list)

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <div className="text-label text-muted">Case Floor</div>
      <h1 className="text-h1 mt-1">Every case, where it stands.</h1>
      <p className="text-body text-muted mt-4 max-w-[62ch]">Cases arrive mixed: a silent Soundbox, a late settlement, a loan question. Each becomes its own case with its own steps. Open one to see what was said and what was done.</p>

      {cases.error && <p role="alert" className="text-bad mt-8">Cases could not be read: {cases.error}. Is the backend running on port 8000?</p>}
      {cases.data && list.length === 0 && <p className="mt-10 text-muted">No cases yet. Open Judge Mode and send a message as a merchant: the case will turn in here.</p>}

      <div className="mt-10 space-y-12">
        {columns.map((col) => (
          <section key={col.state} aria-label={`${nice(col.state)} cases`}>
            <div className="rule-b flex items-baseline gap-3 pb-2">
              <h2 className={`text-label ${col.state === 'ESCALATED' ? 'text-stamp' : 'text-fg'}`}>{nice(col.state)}</h2>
              <span className="text-num text-muted">{col.cases.length}</span>
              {col.state === 'ESCALATED' && <span className="text-small text-muted">a person has these</span>}
            </div>
            <ul>
              {col.cases.map((c) => {
                const fresh = known.current !== null && !known.current.has(c.case_id)
                const isOpen = open === c.case_id
                return (
                  <motion.li key={c.case_id} className="rule-b" initial={fresh ? { opacity: 0, y: m.y(14) } : false} animate={{ opacity: 1, y: 0 }} transition={{ duration: m.d(0.5), ease: ease.out }}>
                    <button type="button" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : c.case_id)}
                      className="grid w-full grid-cols-[6rem_1fr_auto] items-center gap-x-5 gap-y-1 py-4 text-left transition-colors duration-150 hover:bg-surface md:grid-cols-[6rem_1fr_7rem_5rem_9rem]">
                      <span className="text-num text-ink">{c.case_id}</span>
                      <span><span className="text-h3">{nice(c.playbook_id)}</span><span className="text-small text-muted block">{c.merchant_id}{c.step ? ` · step ${c.step}` : ''}</span></span>
                      <span className="text-label text-muted hidden md:block">{c.lane} · {c.language}</span>
                      <span className="text-num text-muted hidden md:block">{c.touches} {c.touches === 1 ? 'touch' : 'touches'}</span>
                      <span className="justify-self-end">{col.state === 'ESCALATED' ? <Stamp tone="stamp">with a person</Stamp> : <Stamp tone={STATE_TONE[col.state] ?? 'ink'}>{nice(col.state)}</Stamp>}</span>
                    </button>
                    {isOpen && <div className="border-t border-line bg-surface"><CaseBody c={c} /></div>}
                  </motion.li>
                )
              })}
            </ul>
          </section>
        ))}
      </div>
    </div>
  )
}
