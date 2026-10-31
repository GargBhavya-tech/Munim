import { useState, type FormEvent, type ReactNode } from 'react'
import { useResource } from '@old/hooks/live'
import { addNote, getDesk, getReps, getWork, moveLead, sendContact, words, type ContactResult, type WorkItem } from '../sales'
import { field, loading, Note, Page, Section } from '../parts'
import { Button, Stamp, Tag } from '../ui'
import { SaveBox, StyleBox } from './Sales'

/** The sales desk: a rep picks who they are, sees their ranked work, opens one card and does the work there. Every button is a real server call; every refusal (consent, do-not-disturb,
 *  quiet hours, the contact budget, the one save attempt) comes back as the answer and is shown as it is. All data is synthetic. */

function Act({ onRun, children, variant }: { onRun: () => Promise<unknown>; children: ReactNode; variant?: 'primary' }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Button variant={variant} disabled={busy} onClick={() => { setBusy(true); setErr(null); onRun().catch((e: Error) => setErr(e.message)).finally(() => setBusy(false)) }}>{children}</Button>
      {err && <span role="alert" className="text-small text-bad">{err}</span>}
    </span>
  )
}

function Contact({ r }: { r: ContactResult }) {
  const sent = r.status === 'SENT' || r.status === 'ROUTED_TO_FIELD'
  return (
    <div role="status" className="border border-line bg-surface p-3">
      <Tag tone={sent ? 'ok' : 'warn'}>{words(r.status)}</Tag>
      {r.channel && <span className="text-small ml-2 text-muted">on {words(r.channel)}</span>}
      {r.body && <p className="text-small mt-2">{r.body}</p>}
      {r.reasons.map((x) => <p key={x} className="text-small mt-1 text-muted">{x}</p>)}
    </div>
  )
}

function LeadCard({ leadId, rep, onChanged }: { leadId: string; rep: string; onChanged: () => void }) {
  const [n, setN] = useState(0)
  const d = useResource(() => getDesk(leadId), [leadId, n])
  const [res, setRes] = useState<ContactResult | null>(null)
  const [text, setText] = useState('')
  const v = d.data
  const refresh = () => { setN((x) => x + 1); onChanged() }
  if (d.error && !v) return <Note error={d.error} />
  if (!v) return loading
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!text.trim()) return
    await addNote(leadId, text.trim(), rep || undefined)
    setText('')
    refresh()
  }
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-title">{v.name}</h3>
        <span className="text-num text-muted">{v.lead_id}</span>
        <Tag tone="ink">{words(v.state)}</Tag>
        {v.do_not_contact && <Stamp tone="bad">do not disturb</Stamp>}
      </div>
      <p className="text-small text-muted">Consented to: {v.consented_channels.length ? v.consented_channels.map(words).join(', ') : 'nothing yet, so no message can go'}</p>

      <div className="space-y-3">
        <h4 className="text-label text-ink">1. Say hello</h4>
        <Act variant="primary" onRun={async () => { setRes(await sendContact(leadId)); refresh() }}>Send the first contact</Act>
        {!v.can_send_first_contact && <p className="text-small text-muted">Munim will probably refuse this one; press it to see exactly why.</p>}
        {res && <Contact r={res} />}
      </div>

      <div className="space-y-3">
        <h4 className="text-label text-ink">2. Log what happened</h4>
        {v.next_states.length ? (
          <div className="flex flex-wrap gap-2">
            {v.next_states.map((s) => <Act key={s} onRun={async () => { await moveLead(leadId, s, `logged by ${rep || 'a rep'}`); refresh() }}>{words(s)}</Act>)}
          </div>
        ) : <p className="text-small text-muted">This lead is finished: nothing more to log.</p>}
        {v.history.length > 0 && (
          <ol className="text-small space-y-1 text-muted">
            {v.history.map((h, i) => <li key={i}>{words(h.from_state)} to {words(h.to_state)}{h.reason ? `, ${h.reason}` : ''}</li>)}
          </ol>
        )}
      </div>

      <div className="space-y-3">
        <h4 className="text-label text-ink">3. How to speak to them</h4>
        <StyleBox id={leadId} />
      </div>

      <form onSubmit={submit} className="space-y-3">
        <h4 className="text-label text-ink">4. Notes</h4>
        <label className="block"><span className="sr-only">Note</span>
          <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={300} rows={2} className={`${field} w-full`} placeholder="What you learned. Never write an OTP, PIN or card number." />
        </label>
        <Button type="submit">Add note</Button>
        {v.notes.map((x) => <p key={x.note_id} className="text-small border-l border-line pl-3">{x.text}</p>)}
      </form>
    </div>
  )
}

function MerchantCard({ item }: { item: WorkItem }) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-title">{item.name}</h3>
        <span className="text-num text-muted">{item.item_id}</span>
        <Tag tone="stamp">at risk</Tag>
      </div>
      <ul className="text-small list-disc space-y-1 pl-5 text-muted">{item.why.map((x) => <li key={x}>{x}</li>)}</ul>
      <p className="text-small">You get one save attempt on this merchant. Plan it here; Munim will refuse a second.</p>
      <SaveBox mid={item.item_id} />
    </div>
  )
}

export function Desk(): ReactNode {
  const reps = useResource(getReps, [])
  const [rep, setRep] = useState('')
  const [open, setOpen] = useState<WorkItem | null>(null)
  const [tick, setTick] = useState(0)
  const work = useResource(getWork, [tick])
  const items = work.data?.items ?? []
  const me = reps.data?.reps.find((r) => r.rep_id === rep)
  return (
    <Page eyebrow="Sales desk" title="Pick a name and work the list." intro="Your ranked work, best first. Open one, and do the work on the card: send the approved first contact, log what happened, plan the one save, add a note. Anything Munim must refuse, it refuses out loud. All data is synthetic.">
      <Section title="Who are you?">
        {reps.error && !reps.data ? <Note error={reps.error} /> : !reps.data ? loading : (
          <label className="block"><span className="text-label text-muted">Rep</span>
            <select value={rep} onChange={(e) => setRep(e.target.value)} className={`${field} mt-1 block w-full max-w-sm`}>
              <option value="">Choose…</option>
              {reps.data.reps.map((r) => <option key={r.rep_id} value={r.rep_id}>{r.name}</option>)}
            </select>
          </label>
        )}
        {me && <p className="text-small mt-3 text-muted">Earned so far: {me.statement.earned_paise / 100} rupees, with {me.statement.pending_paise / 100} still to come as merchants settle.</p>}
      </Section>

      <Section title="Your work">
        {work.error && !work.data ? <Note error={work.error} /> : !work.data ? loading : (
          <div className="grid gap-6 lg:grid-cols-[22rem_1fr]">
            <ol className="max-h-[36rem] space-y-1 overflow-y-auto" aria-label="Ranked work">
              {items.slice(0, 40).map((i, n) => (
                <li key={`${i.kind}-${i.item_id}`}>
                  <button type="button" onClick={() => setOpen(i)} aria-current={open?.item_id === i.item_id}
                    className={`rule-b min-h-12 w-full px-2 py-2 text-left hover:bg-surface ${open?.item_id === i.item_id ? 'bg-surface font-semibold' : ''}`}>
                    <span className="text-num text-muted">{n + 1}</span> {i.name}
                    <span className="text-small block text-muted">{i.kind === 'merchant_at_risk' ? 'At risk' : 'New lead'} · {words(i.channel)}</span>
                  </button>
                </li>
              ))}
            </ol>
            <div>
              {!open ? <p className="text-small text-muted">Choose someone from the list.</p>
                : open.kind === 'new_lead' ? <LeadCard key={open.item_id} leadId={open.item_id} rep={rep} onChanged={() => setTick((x) => x + 1)} />
                : <MerchantCard key={open.item_id} item={open} />}
            </div>
          </div>
        )}
      </Section>
    </Page>
  )
}
