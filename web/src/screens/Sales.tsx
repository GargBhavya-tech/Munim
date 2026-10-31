import { useState, type FormEvent, type ReactNode } from 'react'
import { useResource } from '@old/hooks/live'
import {
  APPROACH_WORD, applyOffer, closeSave, getDips, getReps, getSave, getSightings, getStyle, getWork, paise, postSighting, reviewSighting, signOff, startSave, words,
  type DipFinding, type RepView, type SaveView, type StyleView, type WorkItem,
} from '../sales'
import { Mark } from '../plain'
import { field, loading, Note, Page, Section } from '../parts'
import { Button, Readout, Stamp, Tag, type Tone } from '../ui'

/** Sales: how Munim works out who to talk to, why, and how, before it says a word. Every number, ranking and plan here is the server's; the screen explains each step and shows the
 *  real data behind it. The few actions (record a rival-QR sighting, run the one save attempt, sign an offer off) go through the server like any other. All data is synthetic. */

const DX_TONE: Record<string, Tone> = { COMPETITOR_SWITCH: 'stamp', UNEXPLAINED_DROP: 'warn', ZERO_ACTIVITY: 'warn', DEVICE_OFFLINE: 'muted', SETTLEMENT_FAILURE: 'muted', AREA_WIDE_DIP: 'ok', AREA_OUTAGE: 'ok' }

function Explain({ title, children }: { title: string; children: ReactNode }) {
  return (
    <aside className="border border-line bg-surface p-4">
      <h3 className="text-label text-ink">{title}</h3>
      <div className="text-small mt-2 space-y-2">{children}</div>
    </aside>
  )
}

function Run({ onRun, children, variant }: { onRun: () => Promise<unknown>; children: ReactNode; variant?: 'primary' }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Button variant={variant} disabled={busy} onClick={() => { setBusy(true); setErr(null); onRun().catch((e: Error) => setErr(e.message)).finally(() => setBusy(false)) }}>{children}</Button>
      {err && <span role="alert" className="text-small text-bad">{err}</span>}
    </span>
  )
}

// ---------------------------------------------------------------------------------------------------- 0. the way Munim thinks
function Way() {
  const steps = [
    ['Is the dip real?', 'Compare the merchant with their own normal, then with their neighbours, and wait to see if it lasts.'],
    ['Why is it happening?', 'A broken device is a service case, failing settlements go to support, a rival’s QR means a switch we fight for.'],
    ['Who gets human time?', 'Merchants at risk and new leads, ranked together by the money at stake per rupee of effort.'],
    ['One save, then let go', 'A rep with a briefing or an offer inside the approved limit. One attempt. A bigger offer needs a sign-off.'],
    ['How to approach a lead', 'The more valuable and the less known, the more human. A voice note, a data-led email, a rep, or a person.'],
    ['Won at the first settlement', 'A signup is a win only when the first settlement reaches the bank, and a rep’s incentive follows activity.'],
  ]
  return (
    <ol className="grid gap-4 md:grid-cols-3">
      {steps.map(([t, d], i) => (
        <li key={t} className="border border-line p-4">
          <div className="text-label text-muted">Step {i + 1}</div>
          <div className="text-h3 mt-1">{t}</div>
          <p className="text-small mt-2 text-muted">{d}</p>
        </li>
      ))}
    </ol>
  )
}

// ---------------------------------------------------------------------------------------------------- 1 and 2. the dip, and why
function DipRow({ f }: { f: DipFinding }) {
  const r = f.detail.ratios ?? []
  return (
    <li className="rule-b py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span><b>{f.name ?? f.merchant_id}</b> <span className="text-num text-small text-muted">{f.merchant_id}</span></span>
        <Stamp tone={DX_TONE[f.diagnosis] ?? 'muted'}>{words(f.diagnosis)}</Stamp>
      </div>
      <p className="text-small mt-1"><b>{f.what}.</b> <span className="text-muted">{f.why}.</span></p>
      {r.length > 0 && (
        <p className="text-small mt-1 text-muted">
          Payments against their own normal on the last {r.length} completed days: <b className="text-num text-fg">{r.join(', ')}</b> (1 is normal)
          {f.detail.area_median_ratio !== undefined && f.detail.area_median_ratio !== null && <>; the neighbours stood at <b className="text-num text-fg">{f.detail.area_median_ratio}</b></>}.
        </p>
      )}
      {f.quiet && <p className="text-small mt-1"><Mark mood="ok">Munim says nothing to this merchant</Mark></p>}
    </li>
  )
}

function Dips() {
  const d = useResource(getDips, [])
  const v = d.data
  const group = (pred: (f: DipFinding) => boolean) => (v?.findings ?? []).filter(pred)
  return (
    <Section title="1 and 2. Is the dip real, and why?" note="Munim does not guess that a rival poached someone. It asks whether the dip is even real, then why.">
      <div className="grid gap-4 md:grid-cols-3">
        <Explain title="Against their own normal">The last few completed days are compared with the same weekdays before, with festivals and month-ends allowed for, so a Sunday is not judged against a Monday.</Explain>
        <Explain title="Against their neighbours">If the whole pincode dipped (rain, a bandh), nothing is wrong with this merchant and Munim stays quiet.</Explain>
        <Explain title="And does it last?">{v ? `Only a dip below ${Math.round(v.rules.drop_ratio * 100)}% of normal on each of the last ${v.rules.persist_days} completed days counts.` : 'A one-day dip is noise.'}</Explain>
      </div>
      {d.error && !v ? <Note error={d.error} /> : !v ? loading : (
        <>
          <div className="mt-6 grid gap-x-8 gap-y-4 sm:grid-cols-2 lg:grid-cols-4">
            <Readout label="Merchants checked" value={v.checked} tone="ink" />
            <Readout label="A real, lasting dip" value={group((f) => f.retention).length} note="retention work" tone="stamp" />
            <Readout label="Service or support" value={group((f) => f.diagnosis === 'DEVICE_OFFLINE' || f.diagnosis === 'SETTLEMENT_FAILURE').length} note="not a sales matter" tone="muted" />
            <Readout label="Whole area dipped" value={group((f) => f.quiet).length} note="Munim stays quiet" tone="ok" />
          </div>
          <ul className="mt-4">{v.findings.length === 0 ? <li className="text-body py-3 text-muted">No merchant has a lasting dip right now. That is the honest answer, not an empty screen.</li> : v.findings.map((f) => <DipRow key={`${f.merchant_id}-${f.diagnosis}`} f={f} />)}</ul>
          <p className="text-small mt-2 text-muted">As of {v.as_of}. Computed on a throwaway copy of the data, so looking at this changes nothing.</p>
        </>
      )}
    </Section>
  )
}

function Sightings() {
  const [n, setN] = useState(0)
  const s = useResource(getSightings, [n])
  const [f, setF] = useState({ rep_id: 'R-01', merchant_id: 'M-0003', photo_ref: 'photo-001', qr: true, network: 'rival_a', confidence: '0.9' })
  const [err, setErr] = useState<string | null>(null)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setErr(null)
    void postSighting({ rep_id: f.rep_id.trim(), merchant_id: f.merchant_id.trim(), photo_ref: f.photo_ref.trim(), qr_visible: f.qr, network: f.network || null, confidence: parseFloat(f.confidence) }).then(() => setN(n + 1), (x: Error) => setErr(x.message))
  }
  return (
    <Section title="The rival QR in a rep's street photo" note="A rep photographs a shop front. A reader returns only fields (is a QR visible, which network, how sure); code decides. A confident, known rival is confirmed and becomes the fact the dip diagnosis uses. An unsure reading waits for a person. The photo never decides anything.">
      <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
        <label className="text-small text-muted">Rep<br /><input className={`${field} w-24`} value={f.rep_id} onChange={(e) => setF({ ...f, rep_id: e.target.value })} /></label>
        <label className="text-small text-muted">Merchant<br /><input className={`${field} w-28`} value={f.merchant_id} onChange={(e) => setF({ ...f, merchant_id: e.target.value })} /></label>
        <label className="text-small text-muted">Photo<br /><input className={`${field} w-36`} value={f.photo_ref} onChange={(e) => setF({ ...f, photo_ref: e.target.value })} /></label>
        <label className="text-small text-muted">QR visible<br /><select className={field} value={String(f.qr)} onChange={(e) => setF({ ...f, qr: e.target.value === 'true' })}><option value="true">yes</option><option value="false">no</option></select></label>
        <label className="text-small text-muted">Network<br /><select className={field} value={f.network} onChange={(e) => setF({ ...f, network: e.target.value })}><option value="">none</option>{Object.entries(s.data?.rivals ?? {}).map(([k, v]) => <option key={k} value={k}>{v}</option>)}<option value="unknown_net">not on the list</option></select></label>
        <label className="text-small text-muted">Reader sure<br /><input className={`${field} w-20`} inputMode="decimal" value={f.confidence} onChange={(e) => setF({ ...f, confidence: e.target.value })} /></label>
        <Button type="submit" variant="primary">Record the sighting</Button>
      </form>
      {err && <Note error={err} />}
      <ul className="mt-4">
        {(s.data?.sightings ?? []).length === 0 && <li className="text-small text-muted">No sighting recorded yet.</li>}
        {(s.data?.sightings ?? []).map((x) => (
          <li key={x.sighting_id} className="rule-b text-small flex flex-wrap items-baseline justify-between gap-3 py-2">
            <span><b className="text-num">{x.sighting_id}</b> · {x.merchant_id} · {s.data?.rivals[x.network ?? ''] ?? x.network ?? 'no network'} · reader {Math.round(x.confidence * 100)}% sure{x.reason && <span className="text-muted"> · {x.reason}</span>}</span>
            <span className="inline-flex items-center gap-2">
              <Stamp tone={x.status === 'CONFIRMED' ? 'ok' : x.status === 'REJECTED' ? 'bad' : 'warn'}>{words(x.status)}</Stamp>
              {x.status === 'NEEDS_REVIEW' && <>
                <Run onRun={() => reviewSighting(x.sighting_id, 'sales_lead', 'demo', true).then(() => setN(n + 1))}>Accept</Run>
                <Run onRun={() => reviewSighting(x.sighting_id, 'sales_lead', 'demo', false).then(() => setN(n + 1))}>Reject</Run>
              </>}
            </span>
          </li>
        ))}
      </ul>
      <p className="text-small mt-2 text-muted">A sighting is confirmed only at {Math.round((s.data?.min_confidence ?? 0.7) * 100)}% sure or more, with a visible QR and a network on the known list. The networks here are synthetic placeholders.</p>
    </Section>
  )
}

// ---------------------------------------------------------------------------------------------------- 3. where human time goes
function Work({ onPick }: { onPick: (i: WorkItem) => void }) {
  const w = useResource(getWork, [])
  const [all, setAll] = useState(false)
  const v = w.data
  const shown = (all ? v?.items : v?.items.slice(0, 12)) ?? []
  return (
    <Section title="3. Where human time goes" note="Merchants at risk and new leads on ONE list. Both are the same kind of number: monthly volume won or kept, per paise of cost. So an hour of a rep's or an analyst's time goes where it earns the most.">
      <div className="grid gap-4 md:grid-cols-2">
        <Explain title="A new lead">Expected monthly volume, times the chance they switch, times the chance of a later loan, divided by the cost to win them.</Explain>
        <Explain title="A merchant at risk">Their monthly volume, times the chance they leave, times the chance a save works, divided by the cost of trying. A busy kirana on a rival QR can beat a big chain locked into a bank contract.</Explain>
      </div>
      {w.error && !v ? <Note error={w.error} /> : !v ? loading : (
        <>
          <div className="mt-6 grid gap-x-8 gap-y-4 sm:grid-cols-3">
            <Readout label="On the list" value={v.items.length} tone="ink" />
            <Readout label="Merchants at risk" value={v.counts.merchant_at_risk} tone="stamp" />
            <Readout label="New leads" value={v.counts.new_lead} tone="ink" />
          </div>
          <div className="mt-4 overflow-x-auto">
            <table className="text-small w-full min-w-[40rem] border-collapse text-left">
              <thead><tr className="rule-b text-label text-muted"><th className="py-2 pr-3">#</th><th className="pr-3">Who</th><th className="pr-3">Kind</th><th className="pr-3">Value</th><th className="pr-3">Approach</th><th>Why</th></tr></thead>
              <tbody>
                {shown.map((i, n) => (
                  <tr key={`${i.kind}-${i.item_id}`} className="rule-b align-top hover:bg-surface">
                    <td className="py-2 pr-3 text-num text-muted">{n + 1}</td>
                    <td className="pr-3"><button type="button" onClick={() => onPick(i)} className="text-left font-semibold underline-offset-4 hover:underline">{i.name}</button> <span className="text-num text-muted">{i.item_id}</span></td>
                    <td className="pr-3"><Tag tone={i.kind === 'merchant_at_risk' ? 'stamp' : 'ink'}>{i.kind === 'merchant_at_risk' ? 'at risk' : 'new lead'}</Tag></td>
                    <td className="pr-3 text-num">{i.value}</td>
                    <td className="pr-3">{words(i.channel)}</td>
                    <td className="text-muted">{i.why[0]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {v.items.length > 12 && <button type="button" onClick={() => setAll(!all)} className="text-label mt-3 min-h-10 border border-line px-4 text-muted hover:text-fg">{all ? 'Show the top 12 only' : `Show all ${v.items.length}`}</button>}
          <p className="text-small mt-3 text-muted">The chances of leaving ({Object.entries(v.assumptions.p_lose).map(([k, x]) => `${words(k)} ${Math.round(x * 100)}%`).join(', ')}) and of a save working ({Object.entries(v.assumptions.p_save).map(([k, x]) => `${words(k)} ${Math.round(x * 100)}%`).join(', ')}) are assumptions on synthetic data, not measurements.</p>
        </>
      )}
    </Section>
  )
}

// ---------------------------------------------------------------------------------------------------- 4. the one save attempt
export function SaveBox({ mid }: { mid: string }) {
  const [n, setN] = useState(0)
  const r = useResource(() => getSave(mid), [mid, n])
  const v: SaveView | null = r.data
  const refresh = () => setN((x) => x + 1)
  if (r.error && !v) return <Note error={r.error} />
  if (!v) return loading
  const p = v.plan
  const a = v.attempt
  return (
    <div className="mt-4">
      <h3 className="text-h3">{v.name ?? v.merchant_id} <span className="text-num text-small text-muted">{v.merchant_id}</span></h3>
      <p className="text-small text-muted">Monthly volume {paise(v.monthly_paise)} · {v.diagnosis ? words(v.diagnosis) : 'no lasting dip'}</p>
      {!p.eligible ? <p className="text-body mt-3"><Mark mood="wait">{p.refusal}</Mark></p> : (
        <>
          <p className="text-h3 mt-3">{APPROACH_WORD[p.approach ?? ''] ?? p.approach}{p.offer_months ? `, ${p.offer_months} month(s) of fee waiver` : ''}</p>
          <ul className="text-small mt-2 list-disc space-y-1 pl-5">{p.reasons.map((x) => <li key={x}>{x}.</li>)}</ul>
          <h4 className="text-label mt-4 text-muted">What a rep or analyst is told</h4>
          <ul className="mt-1">{p.briefing.map((b) => <li key={b.text} className="rule-b text-small py-1.5">{b.text}. <span className="text-num text-muted">[{b.source}]</span></li>)}</ul>
        </>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-3">
        {a ? (
          <>
            <Stamp tone={a.status === 'DONE' ? (a.outcome === 'SAVED' ? 'ok' : 'muted') : a.status === 'APPROVED' ? 'ok' : 'warn'}>{a.outcome ? words(a.outcome) : words(a.status)}</Stamp>
            {a.status === 'PROPOSED' && <Run onRun={() => signOff(mid, 'sales_lead', 'demo').then(refresh)}>Sign off as a sales lead</Run>}
            {a.status === 'APPROVED' && a.offer_months ? <Run onRun={() => applyOffer(mid).then((x) => { if (x.status !== 'OK' && x.status !== 'REPLAYED') throw new Error(`${x.status}: ${x.error ?? ''}`); refresh() })}>Apply the offer</Run> : null}
            {a.status !== 'DONE' && <>
              <Run onRun={() => closeSave(mid, 'SAVED').then(refresh)}>They stayed</Run>
              <Run onRun={() => closeSave(mid, 'LET_GO').then(refresh)}>Let them go</Run>
            </>}
          </>
        ) : p.eligible ? <Run variant="primary" onRun={() => startSave(mid).then(refresh)}>Start the one save attempt</Run> : null}
      </div>
      <p className="text-small mt-3 text-muted">One attempt per merchant, enforced by the database, not by a promise. If they still want to leave after it, Munim lets them go and never tries again.</p>
    </div>
  )
}

function Save({ picked }: { picked: WorkItem | null }) {
  const [typed, setTyped] = useState('')
  const mid = typed || (picked?.kind === 'merchant_at_risk' ? picked.item_id : '')
  return (
    <Section title="4. One save attempt, then let go" note="Munim fits the save to the person: a rep with a briefing when the volume at stake is worth a day, otherwise an offer. An offer inside the approved limit needs no one. A bigger one waits for a human to sign.">
      <label className="text-small inline-flex items-center gap-2 text-muted">Merchant at risk
        <input className={`${field} w-32`} placeholder={picked?.kind === 'merchant_at_risk' ? picked.item_id : 'pick from the list above'} value={typed} onChange={(e) => setTyped(e.target.value.trim())} />
      </label>
      {mid ? <SaveBox key={mid} mid={mid} /> : <p className="text-body mt-4 text-muted">Click a merchant at risk in the list above, or type an id, to see how Munim would try to keep them.</p>}
    </Section>
  )
}

// ---------------------------------------------------------------------------------------------------- 5. how a lead is approached
export function StyleBox({ id }: { id: string }) {
  const r = useResource(() => getStyle(id), [id])
  const v: StyleView | null = r.data
  if (r.error && !v) return <Note error={r.error} />
  if (!v) return loading
  const s = v.style
  return (
    <div className="mt-4">
      <h3 className="text-h3">{v.name} <span className="text-num text-small text-muted">{v.lead_id}</span></h3>
      <p className="text-h3 mt-2">{v.approach.words}</p>
      <ul className="text-small mt-2 list-disc space-y-1 pl-5">{v.approach.reasons.map((x) => <li key={x}>{x}.</li>)}</ul>
      {v.approach.bumped && <p className="text-small mt-2"><Mark mood="wait">Moved one step toward a person because we know little about this lead</Mark></p>}
      <h4 className="text-label mt-5 text-muted">The first contact: {words(s.kind)} · by {s.owner}</h4>
      <ol className="text-small mt-2 list-decimal space-y-1 pl-5">{s.steps.map((x) => <li key={x}>{x}.</li>)}</ol>
      {s.voice_note && (
        <figure className="mt-4 border border-line p-4">
          <figcaption className="text-label text-muted">The voice note script · {s.voice_note.words} of {v.voice_budget_words} words · about {s.voice_note.seconds_estimate} seconds · {s.voice_note.language}</figcaption>
          <blockquote className="text-voice mt-2">{s.voice_note.script}</blockquote>
        </figure>
      )}
      {s.email && (
        <figure className="mt-4 border border-line p-4">
          <figcaption className="text-label text-muted">The email · subject: {s.email.subject}</figcaption>
          <ul className="mt-2">{s.email.lines.map((l) => <li key={l.text} className="rule-b text-small py-1.5">{l.text} <span className="text-num text-muted">[{l.source}]</span></li>)}</ul>
          <p className="text-small mt-2 text-muted">{s.email.disclosure}</p>
        </figure>
      )}
      {s.gaps.length > 0 && <div className="mt-4"><h4 className="text-label text-muted">Not done, and why</h4><ul className="text-small mt-1 list-disc space-y-1 pl-5 text-muted">{s.gaps.map((g) => <li key={g}>{g}.</li>)}</ul></div>}
    </div>
  )
}

function Style({ picked, leads }: { picked: WorkItem | null; leads: WorkItem[] }) {
  const [typed, setTyped] = useState('')
  const id = typed || (picked?.kind === 'new_lead' ? picked.item_id : leads[0]?.item_id ?? '')
  return (
    <Section title="5. How a lead is approached" note="The more valuable the lead and the less we know them, the more human the first contact. Then the style fits the person: a short plain voice note for a shopkeeper, a data-led email for an online brand, a rep for a high-value shop, a human for a bank.">
      <div className="grid gap-4 md:grid-cols-3">
        <Explain title="Value picks the level">A self-serve link at the bottom, a voice call or WhatsApp in the middle, a planned rep visit at the top.</Explain>
        <Explain title="Not knowing raises it">A lead whose value rests on one signal is a guess, so its first contact moves one step toward a person.</Explain>
        <Explain title="Words come from approved sources">Voice scripts and emails are built by code from the brief and the approved fixed phrases. No model writes them, and a language is never guessed from a region.</Explain>
      </div>
      <label className="text-small mt-5 inline-flex items-center gap-2 text-muted">Lead
        <input className={`${field} w-28`} placeholder={id || 'L-0001'} value={typed} onChange={(e) => setTyped(e.target.value.trim())} />
      </label>
      {id ? <StyleBox key={id} id={id} /> : <p className="text-body mt-4 text-muted">No lead is ranked yet.</p>}
    </Section>
  )
}

// ---------------------------------------------------------------------------------------------------- 6. reps
function RepCard({ r, newMonths, seniorMonths }: { r: RepView; newMonths: number | null; seniorMonths: number | null }) {
  const st = r.statement
  const depth = newMonths !== null && r.experience_months < newMonths ? 'step by step on a big lead' : seniorMonths !== null && r.experience_months >= seniorMonths ? 'three lines on a small lead' : 'five bullets'
  return (
    <div className="border border-line p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-h3">{r.name} <span className="text-num text-small text-muted">{r.rep_id}</span></h3>
        <span className="text-small text-muted">{r.experience_months} months in the job</span>
      </div>
      <p className="text-small mt-1 text-muted">Speaks {Object.entries(r.languages).map(([l, lv]) => `${l} (${lv})`).join(', ')} · briefed {depth}</p>
      <p className="text-small mt-1 text-muted">Win rate by segment, smoothed toward the team&apos;s so a few visits do not decide it: {Object.keys(r.win_rates).length === 0 ? 'no record yet' : Object.entries(r.win_rates).map(([k, x]) => `${k} ${Math.round(x * 100)}%`).join(', ')}</p>
      <div className="mt-3 grid grid-cols-2 gap-4">
        <Readout label="Earned so far" value={paise(st.earned_paise)} tone="ok" />
        <Readout label="Still to earn" value={paise(st.pending_paise)} tone="warn" />
      </div>
      {st.lines.length === 0 ? <p className="text-small mt-3 text-muted">No verified, converted visit is attributed to this rep yet.</p> : (
        <ul className="mt-3">
          {st.lines.map((l) => (
            <li key={l.visit_id} className="rule-b text-small py-2">
              <b>{l.merchant_name ?? l.lead_id}</b> · <Mark mood={l.status === 'ACTIVATED' ? 'ok' : 'wait'}>{l.status === 'ACTIVATED' ? 'counted as a win' : 'not a win yet'}</Mark>
              <p className="text-muted">{l.note}.</p>
              {l.months.length > 0 && <p className="text-muted">Month by month: {l.months.map((m) => m.status === 'COMPLETE' ? `${paise(m.earned_paise)} of ${paise(m.share_paise)}` : 'not yet').join(', ')}</p>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Reps() {
  const r = useResource(getReps, [])
  const v = r.data
  return (
    <Section title="6. When a rep goes, and what counts as a win" note="Reps are matched to leads by language and by their own win rate in that segment. The briefing fits the rep. And a sale is won when the first settlement reaches the bank, not at yes.">
      <div className="grid gap-4 md:grid-cols-3">
        <Explain title="Matched, not assigned">By language first, then by the rep&apos;s own smoothed win rate in that kind of shop, then by distance on a planned route.</Explain>
        <Explain title="A briefing that fits">A new rep on a big lead gets every step. A veteran on a small lead gets three lines.</Explain>
        <Explain title="An incentive that follows activity">{v?.reps[0] ? `${v.reps[0].statement.assumptions.activation_share_pct}% is earned when the first settlement lands; the rest over ${v.reps[0].statement.assumptions.activity_months} months, in step with what the merchant actually processes. A signup that never transacts earns nothing more.` : 'Part is earned at the first settlement, the rest over the first months, in step with what the merchant processes.'}</Explain>
      </div>
      {r.error && !v ? <Note error={r.error} /> : !v ? loading : (
        <div className="mt-5 grid gap-4 md:grid-cols-2">{v.reps.map((x) => <RepCard key={x.rep_id} r={x} newMonths={v.briefing.new_months} seniorMonths={v.briefing.senior_months} />)}</div>
      )}
      <p className="text-small mt-3 text-muted">The incentive pool and shares are assumptions on synthetic data. This is a statement of what is earned; it pays nothing.</p>
    </Section>
  )
}

// ---------------------------------------------------------------------------------------------------- the screen
export function Sales(): ReactNode {
  const [picked, setPicked] = useState<WorkItem | null>(null)
  const work = useResource(getWork, [])
  const leads = (work.data?.items ?? []).filter((i) => i.kind === 'new_lead')
  return (
    <Page eyebrow="Sales" title="Who to talk to, why, and how, before saying a word." intro="Most sales AI sends more messages. Munim first works out whether a merchant is really leaving, why, who is worth human time, and how each person should be approached. Every step below is explained, with the real data behind it. All of it is synthetic, and the chances and costs are assumptions, not measurements.">
      <Way />
      <Dips />
      <Sightings />
      <Work onPick={setPicked} />
      <Save picked={picked} />
      <Style picked={picked} leads={leads} />
      <Reps />
    </Page>
  )
}
