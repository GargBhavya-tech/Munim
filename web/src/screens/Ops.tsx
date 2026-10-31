import { useState, type FormEvent, type ReactNode } from 'react'
import { useLive, useResource } from '@old/hooks/live'
import {
  bookStep, getDay, getFixPlan, getHours, getPeople, hhmm, postException, putHours, RUNG_WORD, rupeesFromPaise, reschedule, scamCheck,
  type DayView, type FixPlanView, type Hours, type ScamReport, type Step,
} from '../ops'
import { Mark, Term } from '../plain'
import { field, fieldSm, loading, Note, Page, Section } from '../parts'
import { Button, Readout, Stamp, Tag, type Tone } from '../ui'

/** Service ops: how Munim climbs the fix ladder, how the field day is planned and re-planned, who is on call, and the scam sandbox. It REPORTS what the service modules decided
 *  (costs, chances and routes are the server's), and the few actions here (book a step, report a shut shop, report a delay) go through the server like any other. Travel
 *  times behind the day plan are SIMULATED. All data is synthetic. */

const pad = (s: string) => s.replace(/_/g, ' ').toLowerCase()
const when = (iso: string) => iso.slice(0, 16).replace('T', ' ')

// ---------------------------------------------------------------------------------------------------- the fix ladder
function PlanBody({ v }: { v: FixPlanView }) {
  const p = v.plan
  const s = v.situation
  return (
    <div>
      <div className="grid gap-x-8 gap-y-5 sm:grid-cols-2 lg:grid-cols-5">
        <Readout label="Device" value={pad(s.device_status)} tone={s.device_status === 'ONLINE' ? 'ok' : 'bad'} />
        <Readout label="Silent for" value={s.silent_hours === null ? 'never seen' : s.silent_hours} unit={s.silent_hours === null ? undefined : 'hours'} tone="ink" />
        <Readout label="Remote fixes tried" value={s.remote_attempts} tone="ink" />
        <Readout label="Replacements, 30 days" value={s.replacements_30d} tone={s.replacements_30d > 0 ? 'warn' : 'ink'} />
        <Readout label="Waiting costs, per hour" value={rupeesFromPaise(p.loss_per_hour_paise)} note={`shop hours: ${pad(s.hours_source)}`} tone="stamp" />
      </div>
      <h3 className="text-h2 mt-8">{RUNG_WORD[p.start_rung]}</h3>
      <ul className="text-body mt-3 list-disc space-y-1 pl-5">{p.reasons.map((r) => <li key={r}>{r}.</li>)}</ul>
      {p.rungs.length > 0 && (
        <div className="mt-6 overflow-x-auto">
          <table className="text-small w-full min-w-[40rem] border-collapse text-left">
            <caption className="text-label pb-2 text-left text-muted">Each rung: what it costs, how likely it is to work, how long it takes, and the expected cost if we start there</caption>
            <thead><tr className="rule-b text-label text-muted"><th className="py-2 pr-3">Rung</th><th className="pr-3">What happens</th><th className="pr-3">Cost</th><th className="pr-3"><Term term="chance of working">Chance</Term></th><th className="pr-3">Hours</th><th>Expected if we start here</th></tr></thead>
            <tbody>
              {p.rungs.map((r) => (
                <tr key={r.rung} className={`rule-b ${r.rung === p.start_rung ? 'bg-surface font-semibold' : ''}`}>
                  <td className="py-2 pr-3 text-num">{r.rung}{r.rung === p.start_rung && <> <Stamp>start</Stamp></>}</td>
                  <td className="pr-3">{r.name}</td>
                  <td className="pr-3 text-num">{rupeesFromPaise(r.cost_paise)}</td>
                  <td className="pr-3 text-num">{Math.round(r.p_success * 100)}%</td>
                  <td className="pr-3 text-num">{r.hours}</td>
                  <td className="text-num">{rupeesFromPaise(r.expected_if_start_here_paise)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-small mt-2 text-muted">The chances, costs and hours are assumptions for this synthetic world, not measurements. If a rung fails the merchant climbs to the next, which is why a cheap rung that rarely works can cost more than starting higher.</p>
        </div>
      )}
    </div>
  )
}

function StepCard({ s, mid, onDone }: { s: Step; mid: string; onDone: () => void }) {
  const [out, setOut] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const book = () => bookStep(mid, s.rung).then((r) => { setOut(r.status === 'OK' ? 'Booked' : r.status === 'REPLAYED' ? 'Already booked' : `${r.status}: ${r.error ?? ''}`); onDone() }, (e: Error) => setErr(e.message))
  const who = s.who === 'munim' ? 'Munim' : s.who === 'helper' ? `${s.name} (${s.mode})` : s.who ? `${s.name} (${pad(s.who)})` : null
  return (
    <li className="border border-line p-4">
      <div className="text-label text-muted">Rung {s.rung}</div>
      {who ? <div className="text-h3 mt-1">{who}</div> : <div className="text-body mt-1 text-muted">{s.unavailable ?? s.note}</div>}
      {s.does && <p className="text-small mt-2">Restarts, resets the volume or pushes settings, then <b>{s.then}</b>.</p>}
      {s.eta_min !== undefined && <p className="text-small mt-2 text-muted">About {s.eta_min} minutes away{s.cost_paise ? `, ${rupeesFromPaise(s.cost_paise)}` : ''}.</p>}
      {s.booking && <p className="text-small mt-1 text-muted">Would book a {s.booking.args.kind} at <b className="text-num text-fg">{when(s.booking.args.slot_at)}</b>.</p>}
      {s.why && <ul className="text-small mt-2 list-disc pl-5 text-muted">{s.why.map((w) => <li key={w}>{w}</li>)}</ul>}
      {s.rejected && Object.keys(s.rejected).length > 0 && <p className="text-small mt-2 text-muted">Passed over: {Object.entries(s.rejected).map(([k, v]) => `${k} (${pad(v)})`).join(', ')}.</p>}
      {s.booking && <div className="mt-3"><Button onClick={() => void book()}>Book this</Button>{out && <span className="text-small ml-3"><Mark mood="ok">{out}</Mark></span>}</div>}
      {err && <p role="alert" className="text-small mt-2 text-bad">{err}</p>}
    </li>
  )
}

function ShopHours({ mid }: { mid: string }) {
  const r = useResource(() => getHours(mid), [mid])
  const [h, setH] = useState<Hours | null>(null)
  const [open, setOpen] = useState('')
  const [close, setClose] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const shown = h ?? r.data
  const save = (e: FormEvent) => {
    e.preventDefault()
    setErr(null)
    void putHours(mid, { open: open || shown?.open || '', close: close || shown?.close || '', closed_days: shown?.closed_days ?? [] }).then(setH, (x: Error) => setErr(x.message))
  }
  if (r.error && !shown) return <Note error={r.error} />
  if (!shown) return loading
  return (
    <form onSubmit={save} className="text-small flex flex-wrap items-center gap-3">
      <span>Shop hours: <b className="text-num">{shown.open} to {shown.close}</b> <Tag tone={shown.source === 'REPORTED' ? 'ok' : 'warn'}>{shown.source === 'REPORTED' ? 'reported' : 'assumed: a typical day'}</Tag></span>
      <label className="inline-flex items-center gap-2 text-muted">Open <input className={`${fieldSm} w-24`} placeholder={shown.open} value={open} onChange={(e) => setOpen(e.target.value)} /></label>
      <label className="inline-flex items-center gap-2 text-muted">Close <input className={`${fieldSm} w-24`} placeholder={shown.close} value={close} onChange={(e) => setClose(e.target.value)} /></label>
      <Button type="submit" className="min-h-9 px-3">Report these hours</Button>
      {err && <span role="alert" className="text-bad">{err}</span>}
    </form>
  )
}

function FixPlanner() {
  const { api } = useLive()
  const cases = useResource(() => api.cases(), [api])
  const ids = [...new Set((cases.data?.cases ?? []).map((c) => c.merchant_id))].sort()
  const [mid, setMid] = useState('M-0005')
  const [draft, setDraft] = useState('M-0005')
  const [n, setN] = useState(0)
  const plan = useResource(() => getFixPlan(mid), [mid, n])
  return (
    <Section title="The fix ladder" note="Pick a merchant with a Soundbox fault. Munim weighs the cost of each fix, how likely it is to work and what the merchant loses while waiting, and starts on the cheapest rung.">
      <form onSubmit={(e) => { e.preventDefault(); setMid(draft.trim()) }} className="flex flex-wrap items-center gap-3">
        <label className="text-small inline-flex items-center gap-2 text-muted">Merchant
          <input list="ops-merchants" className={`${field} w-32`} value={draft} onChange={(e) => setDraft(e.target.value)} />
          <datalist id="ops-merchants">{[...new Set([...ids, 'M-0001', 'M-0003', 'M-0005', 'M-0006', 'M-0010'])].map((i) => <option key={i} value={i} />)}</datalist>
        </label>
        <Button type="submit" variant="primary">Plan the fix</Button>
      </form>
      <div className="mt-4"><ShopHours key={mid} mid={mid} /></div>
      <div className="mt-6">
        {plan.error && !plan.data ? <Note error={plan.error} /> : !plan.data ? loading : (
          <>
            <PlanBody v={plan.data} />
            {plan.data.people.steps.length > 0 && (
              <>
                <h3 className="text-label mt-8 text-muted">Who does each rung</h3>
                <ol className="mt-2 grid gap-4 md:grid-cols-3">{plan.data.people.steps.map((s, i) => <StepCard key={`${s.rung}-${i}`} s={s} mid={mid} onDone={() => setN(n + 1)} />)}</ol>
              </>
            )}
          </>
        )}
      </div>
    </Section>
  )
}

// ---------------------------------------------------------------------------------------------------- the field day
function DayBody({ v, names, act, busy }: { v: DayView; names: Record<string, string>; act: (f: () => Promise<unknown>) => void; busy: boolean }) {
  const people = Object.keys(v.routes)
  return (
    <div>
      <p className="text-small text-muted">Day {v.day}. {v.simulated && <b className="text-fg">Travel times are simulated.</b>} Each person is planned from {Object.values(v.starts).includes('live position') ? 'where they are now, or their base' : 'their base'}; a visit is placed inside the hours the shop is open.</p>
      {people.length === 0 && <p className="text-body mt-4 text-muted">No visit is planned for this day.</p>}
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        {people.map((id) => (
          <div key={id} className="border border-line p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-h3">{names[id] ?? id} <span className="text-num text-small text-muted">{id}</span></h3>
              <Button className="min-h-9 px-3 text-small" disabled={busy} onClick={() => act(() => postException({ kind: 'RUNNING_LONG', person_id: id, minutes: 30 }))}>Running 30 min late</Button>
            </div>
            <p className="text-small text-muted">Starts from {v.starts[id] ?? 'base'}{v.delayed[id] ? `, free ${v.delayed[id]} minutes later than planned` : ''}.</p>
            <ol className="mt-3">
              {v.routes[id].map((s) => (
                <li key={s.job_id} className="rule-b text-small flex flex-wrap items-baseline justify-between gap-x-3 py-2">
                  <span><b className="text-num">{hhmm(s.arrive_min)} to {hhmm(s.leave_min)}</b> · {s.name} <span className="text-num text-muted">{s.merchant_id}</span>{s.late_min > 0 && <span className="text-bad"> · {s.late_min} min late</span>}</span>
                  <Button className="min-h-9 px-3 text-small" disabled={busy} onClick={() => act(() => postException({ kind: 'SHOP_CLOSED', booking_id: s.job_id }))}>Shop is closed</Button>
                </li>
              ))}
            </ol>
          </div>
        ))}
      </div>
      {v.rescheduled.length > 0 && (
        <div className="mt-6">
          <h3 className="text-label text-muted">Moved out of today: the shop was closed</h3>
          <ul className="mt-2">
            {v.rescheduled.map((r) => (
              <li key={r.booking_id} className="rule-b text-small flex flex-wrap items-baseline justify-between gap-3 py-2">
                <span><b>{r.name}</b> <span className="text-num text-muted">{r.merchant_id}</span> · next opening <b className="text-num">{when(r.proposed_slot_at)}</b></span>
                <Button className="min-h-9 px-3 text-small" disabled={busy} onClick={() => act(() => reschedule(r.booking_id))}>Book the new visit</Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {v.unassigned.length > 0 && (
        <div className="mt-6">
          <h3 className="text-label text-muted">Nobody can take these, and why</h3>
          <ul className="mt-2">
            {v.unassigned.map((u) => (
              <li key={u.job_id} className="rule-b text-small py-2"><span className="text-num">{u.job_id}</span> · {u.merchant_id} · <b>{pad(u.reason)}</b>{u.rejected && <span className="text-muted"> ({Object.entries(u.rejected).map(([k, x]) => `${k}: ${pad(x)}`).join(', ')})</span>}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function FieldDay() {
  const day = useResource(getDay, [])
  const people = useResource(getPeople, [])
  const [local, setLocal] = useState<DayView | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const names = Object.fromEntries((people.data?.technicians ?? []).map((t) => [t.tech_id, t.name]))
  const act = (f: () => Promise<unknown>) => {
    setBusy(true)
    setErr(null)
    f().then(async (r) => { const plan = (r as { plan?: DayView })?.plan; setLocal(plan ?? (await getDay())) }, (e: Error) => setErr(e.message)).finally(() => setBusy(false))
  }
  const view = local ?? day.data
  return (
    <Section title="The field day" note="Every visit booked for the day, given to the right person by skill, parts, shift and a durable fix record, then routed. Report a closed shop or a late visit and the whole day is re-planned.">
      {err && <Note error={err} />}
      {day.error && !view ? <Note error={day.error} /> : !view ? loading : <DayBody v={view} names={names} act={act} busy={busy} />}
      <p className="text-small mt-6 text-muted">
        {(people.data?.positions.length ?? 0) === 0
          ? 'Nobody is reporting a position right now. A position is recorded only during a person’s shift on an active route, and only the latest recent one is shown.'
          : `Where people are now: ${people.data?.positions.map((p) => `${p.name ?? p.person_id} (${p.age_min} min ago)`).join(', ')}.`}
      </p>
    </Section>
  )
}

// ---------------------------------------------------------------------------------------------------- on call
function OnCall() {
  const people = useResource(getPeople, [])
  const d = people.data
  return (
    <Section title="On call and on the road" note="Who can guide a merchant by voice or video, and who can bring a replacement unit. These rosters are synthetic.">
      {people.error && !d ? <Note error={people.error} /> : !d ? loading : (
        <div className="grid gap-8 md:grid-cols-2">
          <div>
            <h3 className="text-label text-muted">Helpers (rung two)</h3>
            {d.helpers.length === 0 ? <p className="text-small mt-2 text-muted">No helper is on the roster.</p> : (
              <ul className="mt-2">{d.helpers.map((h) => (
                <li key={h.helper_id} className="rule-b text-small py-2"><b>{h.name}</b> <span className="text-num text-muted">{h.helper_id}</span> · on call {h.on_call[0]} to {h.on_call[1]} · {h.modes.join(' and ')}<br /><span className="text-muted">{Object.entries(h.languages).map(([l, lv]) => `${l} ${lv}`).join(', ')}</span></li>
              ))}</ul>
            )}
          </div>
          <div>
            <h3 className="text-label text-muted">Couriers and certified people (a swap without a technician)</h3>
            {d.couriers.length === 0 ? <p className="text-small mt-2 text-muted">No courier is on the roster.</p> : (
              <ul className="mt-2">{d.couriers.map((c) => <li key={c.courier_id} className="rule-b text-small py-2"><b>{c.name}</b> <span className="text-num text-muted">{c.courier_id}</span> · shift {c.shift[0]} to {c.shift[1]} · carries {c.units} units</li>)}</ul>
            )}
            <p className="text-small mt-3 text-muted">Certified for swaps: {d.technicians.filter((t) => t.certifications.includes('soundbox_l1') && (t.parts.soundbox_unit ?? 0) > 0).map((t) => t.name).join(', ') || 'nobody'}.</p>
          </div>
        </div>
      )}
    </Section>
  )
}

// ---------------------------------------------------------------------------------------------------- the scam sandbox
const VERDICT_TONE: Record<string, Tone> = { REAL: 'ok', LIKELY_SCAM: 'bad', CANNOT_TELL: 'warn' }
const VERDICT_WORD: Record<string, string> = { REAL: 'Looks real', LIKELY_SCAM: 'Looks like a scam', CANNOT_TELL: 'Cannot tell: a person looks' }

function ScamBox() {
  const [mid, setMid] = useState('M-0002')
  const [rupees, setRupees] = useState('')
  const [ref, setRef] = useState('')
  const [at, setAt] = useState('')
  const [rep, setRep] = useState<ScamReport | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const run = (e: FormEvent) => {
    e.preventDefault()
    setErr(null)
    setRep(null)
    const paise = Math.round(parseFloat(rupees) * 100)
    if (!Number.isFinite(paise) || paise <= 0) { setErr('Enter the amount the customer says they paid, in rupees.'); return }
    const claimed = at.trim() ? (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(at.trim()) ? `${at.trim()}:00+05:30` : at.trim()) : undefined
    void scamCheck({ merchant_id: mid.trim(), amount_paise: paise, reference: ref.trim() || undefined, claimed_at: claimed }).then(setRep, (x: Error) => setErr(x.message))
  }
  return (
    <Section title="Is this payment claim real?" note="A customer says they paid. Munim puts the claim into a throwaway copy of the merchant's books, sees what breaks, and throws the copy away. Nothing real is changed.">
      <form onSubmit={run} className="flex flex-wrap items-end gap-3">
        <label className="text-small text-muted">Merchant<br /><input className={`${field} w-28`} value={mid} onChange={(e) => setMid(e.target.value)} /></label>
        <label className="text-small text-muted">Amount, rupees<br /><input className={`${field} w-32`} inputMode="decimal" value={rupees} onChange={(e) => setRupees(e.target.value)} /></label>
        <label className="text-small text-muted">Reference (optional)<br /><input className={`${field} w-52`} placeholder="UPI and 12 digits" value={ref} onChange={(e) => setRef(e.target.value)} /></label>
        <label className="text-small text-muted">Time (optional)<br /><input className={`${field} w-52`} placeholder="2026-10-31T11:00" value={at} onChange={(e) => setAt(e.target.value)} /></label>
        <Button type="submit" variant="primary">Check the claim</Button>
      </form>
      {err && <Note error={err} />}
      {rep && (
        <div className="mt-5">
          <Stamp tone={VERDICT_TONE[rep.verdict] ?? 'muted'}>{VERDICT_WORD[rep.verdict] ?? rep.verdict}</Stamp>
          <ul className="text-body mt-3 list-disc space-y-1 pl-5">{rep.signals.length === 0 ? <li>No signal either way.</li> : rep.signals.map((s) => <li key={s.code}>{s.text}. <span className="text-small text-muted">({s.severity === 'hard' ? 'a contradiction' : 'a hint'})</span></li>)}</ul>
          <p className="text-small mt-3 text-muted">Checked in a copy holding {rep.copied.payments} of this merchant&apos;s payments and {rep.copied.references} references. The copy was discarded; the real books were not touched. The merchant is never accused: a person decides.</p>
        </div>
      )}
    </Section>
  )
}

// ---------------------------------------------------------------------------------------------------- the screen
export function Ops(): ReactNode {
  return (
    <Page eyebrow="Service ops" title="Climb only as high as needed." intro="Most Soundbox faults need a restart, not a drive across the city. Munim starts on the cheapest fix that is likely to work, brings in a person only when it must, and plans the field day around shops that are open. Costs and chances here are assumptions on synthetic data.">
      <FixPlanner />
      <FieldDay />
      <OnCall />
      <ScamBox />
    </Page>
  )
}
