import { useState, type ReactNode } from 'react'
import { useLive, useResource } from '@old/hooks/live'
import { useFlash } from '@old/hooks/useFlash'
import type { Case } from '@old/api/types'
import { causesByTile, type TileCause, type TileId } from '@old/lib/systems'
import { LANGUAGE_NAMES, PLAYBOOK_NAMES } from '../glossary'
import { Mark, num, rupees, str, Term, type Mood } from '../plain'
import { fmtSim } from '../shell'
import { Counter, Stamp } from '../ui'
import { fieldSm, Json, loading, Note, Page } from '../parts'

/** The shop floor: the mock Paytm systems for one merchant, read fresh from the server on every ledger event. Each card says what the data MEANS first and keeps the raw
 *  record one click away. A card that just changed lights up and names the action that caused it. Nothing here is computed: every value is the server's. */

type Rec = Record<string, unknown>
const rec = (v: unknown): Rec => (v && typeof v === 'object' ? (v as Rec) : {})

function Tile({ id, title, data, cause, lead, wide = false, children }: { id: TileId; title: string; data: unknown; cause?: TileCause; lead?: ReactNode; wide?: boolean; children?: ReactNode }) {
  const flash = useFlash(data)
  return (
    <section
      data-tile={id}
      aria-label={title}
      data-flash={flash ? 'on' : 'off'}
      className={`border p-5 transition-colors duration-500 ${wide ? 'md:col-span-2' : ''} ${flash ? 'border-stamp bg-surface' : 'border-line'}`}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-label text-muted">{title}</h2>
        {flash && <Stamp>just changed</Stamp>}
      </div>
      {lead && <p className="text-h3 mt-3">{lead}</p>}
      {children && <div className="mt-3 space-y-2">{children}</div>}
      {cause && (
        <p className="text-small mt-4 text-muted">
          Last changed by <b className="text-num text-fg">{cause.action}</b>
          {cause.key && <> · <Term term="idempotency key">key</Term> <code className="text-num">{cause.key.slice(0, 12)}</code></>}
        </p>
      )}
      <details className="mt-3">
        <summary className="text-label min-h-9 cursor-pointer text-muted hover:text-fg">Show raw data</summary>
        <Json value={data} />
      </details>
    </section>
  )
}

// ---------------------------------------------------------------------------------------------------- Soundbox
function SoundboxArt({ online }: { online: boolean }) {
  return (
    <svg viewBox="0 0 170 120" role="img" aria-label={online ? 'Soundbox, online' : 'Soundbox, offline'} className={`h-28 w-auto shrink-0 ${online ? 'text-ok' : 'text-bad'}`}>
      <rect x="30" y="28" width="82" height="74" rx="8" fill="none" stroke="currentColor" strokeWidth="3" strokeDasharray={online ? undefined : '6 5'} />
      {[0, 1, 2].flatMap((r) => [0, 1, 2].map((c) => <circle key={`${r}${c}`} cx={51 + c * 20} cy={58 + r * 15} r="3.2" fill="currentColor" opacity="0.55" />))}
      <circle cx="71" cy="42" r="4" fill={online ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="2" />
      {online
        ? [14, 26, 38].map((r, i) => (
            <path key={`${r}-on`} className="soundbox-ring" style={{ animationDelay: `${i * 120}ms` }} d={`M ${120} ${65 - r} A ${r} ${r} 0 0 1 ${120} ${65 + r}`} fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
          ))
        : <path d="M 122 55 l 18 20 M 140 55 l -18 20" stroke="currentColor" strokeWidth="3" strokeLinecap="round" fill="none" />}
    </svg>
  )
}

function Meter({ label, pct }: { label: string; pct: number | null }) {
  const v = pct === null ? 0 : Math.max(0, Math.min(100, pct))
  return (
    <div className="text-small">
      <div className="flex justify-between gap-3"><span className="text-muted">{label}</span><span className="text-num">{pct === null ? 'unknown' : `${pct}%`}</span></div>
      <div className="mt-1 h-2 border border-line" role="img" aria-label={`${label} ${pct === null ? 'unknown' : `${pct} percent`}`}>
        <div className="h-full bg-ink" style={{ width: `${v}%` }} />
      </div>
    </div>
  )
}

function SoundboxCard({ device, cause }: { device: Rec; cause?: TileCause }) {
  const status = str(device.status)
  const online = status === 'ONLINE'
  const beat = str(device.last_heartbeat)
  const test = device.last_test_announcement_ok
  return (
    <Tile id="device" title="Soundbox" data={device} cause={cause}
      lead={status === null ? 'No device on record' : <Mark mood={online ? 'ok' : 'bad'}>{online ? 'Online and ready to announce payments' : status === 'OFFLINE' ? 'Offline: it cannot announce payments' : status}</Mark>}>
      <div className="flex items-center gap-5">
        <SoundboxArt key={status ?? 'none'} online={online} />
        <div className="min-w-0 flex-1 space-y-3">
          <Meter label="Battery" pct={num(device.battery_pct)} />
          <Meter label="Signal" pct={num(device.signal_pct)} />
        </div>
      </div>
      <p className="text-small text-muted">
        Last <Term term="heartbeat">check-in</Term>: <b className="text-fg">{beat ? fmtSim(beat) : 'never'}</b>
        {str(device.model) && <> · {str(device.model)}</>}
      </p>
      {typeof test === 'number' && <p className="text-small text-muted">Test announcement: <b className="text-fg">{test === 1 ? 'worked' : 'did not work'}</b></p>}
    </Tile>
  )
}

// ---------------------------------------------------------------------------------------------------- Money
interface Path { at: 0 | 1 | 2; mood: Mood; text: ReactNode }

function pathFor(s: Rec | null): Path | null {
  if (!s) return null
  const status = str(s.status)
  const amt = rupees(s.amount_paise)
  if (status === 'SETTLED') return { at: 2, mood: 'ok', text: <>{amt} reached the bank account</> }
  if (status === 'HELD_RISK') return { at: 1, mood: 'wait', text: <>{amt} is <Term term="HELD_RISK">held for a safety check</Term></> }
  if (status === 'SCHEDULED') return { at: 1, mood: 'wait', text: <>{amt} is <Term term="SCHEDULED">scheduled</Term> for payout</> }
  if (status === 'PENDING') return { at: 1, mood: 'wait', text: <>{amt} is <Term term="PENDING">waiting</Term> to be paid out</> }
  if (status === 'FAILED') return { at: 1, mood: 'bad', text: <>The {amt} payout failed</> }
  return { at: 1, mood: 'none', text: <>Settlement {status ?? 'status unknown'}: {amt}</> }
}

function MoneyPipeline({ at, mood }: { at: 0 | 1 | 2; mood: Mood }) {
  const nodes = ['Customer pays', 'Paytm holds it', 'Shop’s bank account']
  const here = mood === 'bad' ? 'border-bad bg-bad text-on-stamp' : mood === 'wait' ? 'border-warn bg-warn text-bg' : 'border-ok bg-ok text-bg'
  return (
    <ol className="flex items-stretch gap-0" aria-label="Where the money is">
      {nodes.map((n, i) => (
        <li key={n} className="flex min-w-0 flex-1 items-center">
          <span aria-current={i === at ? 'step' : undefined} className={`text-small w-full border px-2 py-2 text-center font-medium ${i === at ? here : i < at ? 'border-line text-fg' : 'border-line text-muted'}`}>
            {n}{i === at && <span className="sr-only"> (the money is here)</span>}
          </span>
          {i < nodes.length - 1 && <span aria-hidden="true" className="text-num px-1 text-muted">{'→'}</span>}
        </li>
      ))}
    </ol>
  )
}

function MoneyCard({ payments, cause }: { payments: { latest: Rec[]; settlement: Rec | null }; cause?: TileCause }) {
  const s = payments.settlement
  const path = pathFor(s)
  const expected = str(s?.expected_at)
  const settled = str(s?.settled_at)
  const reason = str(s?.reason)
  return (
    <Tile id="payments" title="Money: payments and payout" data={payments} cause={cause} wide
      lead={path ? <Mark mood={path.mood}>{path.text}</Mark> : 'No payout on record'}>
      {path && <MoneyPipeline at={path.at} mood={path.mood} />}
      {s && (
        <p className="text-small text-muted">
          {settled ? <>Paid out <b className="text-fg">{fmtSim(settled)}</b></> : expected ? <>Expected <b className="text-fg">{fmtSim(expected)}</b></> : null}
          {reason && <> · reason: <Term term={reason}>{reason}</Term></>}
        </p>
      )}
      {payments.latest.length > 0 && (
        <div className="pt-1">
          <div className="text-label text-muted">Latest payments from customers</div>
          <ul className="mt-1">
            {payments.latest.slice(0, 5).map((p, i) => (
              <li key={str(p.payment_id) ?? i} className="rule-b text-small flex flex-wrap items-baseline justify-between gap-x-4 py-1.5">
                <span className="text-num font-medium">{rupees(p.amount_paise)}</span>
                <span className="text-muted">{str(p.status) ?? 'unknown'}</span>
                <span className="text-num text-muted">{str(p.ts) ? fmtSim(str(p.ts)) : ''}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Tile>
  )
}

// ---------------------------------------------------------------------------------------------------- Identity and bank
const KYC_MOOD: Record<string, [Mood, string]> = {
  APPROVED: ['ok', 'Identity approved'],
  REJECTED: ['bad', 'Identity check rejected'],
  PENDING: ['wait', 'Identity check in progress'],
  NOT_STARTED: ['wait', 'Identity check not started'],
}
const BANK_MOOD: Record<string, [Mood, string]> = {
  VERIFIED: ['ok', 'Bank account verified'],
  FAILED: ['bad', 'Bank check failed'],
  PENDING: ['wait', 'Bank check in progress'],
  NOT_STARTED: ['wait', 'Bank check not started'],
}

function CheckCard({ id, title, data, table, cause, children }: { id: TileId; title: string; data: Rec; table: Record<string, [Mood, string]>; cause?: TileCause; children?: ReactNode }) {
  const status = str(data.status)
  const [mood, text] = status ? table[status] ?? ['none' as Mood, status] : ['none' as Mood, 'No record']
  const reason = str(data.reason)
  const attempts = num(data.attempts)
  return (
    <Tile id={id} title={title} data={data} cause={cause} lead={<Mark mood={mood}>{text}</Mark>}>
      {reason && <p className="text-small">Why: <b><Term term={reason}>{reason}</Term></b></p>}
      {children}
      {attempts !== null && <p className="text-small text-muted">Attempts so far: <b className="text-num text-fg">{attempts}</b></p>}
    </Tile>
  )
}

// ---------------------------------------------------------------------------------------------------- the rest
const caseName = (c: Case) => PLAYBOOK_NAMES[c.playbook_id] ?? c.playbook_id

function CasesCard({ crm, cause }: { crm: { open_cases: Case[]; focus: string | null; language: string }; cause?: TileCause }) {
  const n = crm.open_cases.length
  return (
    <Tile id="crm" title="Open cases" data={crm} cause={cause} lead={n === 0 ? 'No open cases' : `${n} open ${n === 1 ? 'case' : 'cases'}`}>
      {crm.open_cases.map((c) => (
        <p key={c.case_id} className="text-small rule-b pb-2">
          <b>{caseName(c)}</b> <span className="text-num text-muted">{c.case_id}</span>
          <br />
          <Term term={c.state}>{c.state.toLowerCase()}</Term>
          {c.step && <> · at step <b className="text-num">{c.step}</b></>}
          {' · '}<Term term="lane">lane {c.lane}</Term>
        </p>
      ))}
      <p className="text-small text-muted">Speaks <b className="text-fg">{LANGUAGE_NAMES[crm.language] ?? crm.language}</b></p>
    </Tile>
  )
}

function PricingCard({ pricing, cause }: { pricing: { band: Rec; waivers: unknown[] }; cause?: TileCause }) {
  const offer = str(pricing.band.offer_id)
  const months = num(pricing.band.max_months)
  return (
    <Tile id="pricing" title="Fees and offers" data={pricing} cause={cause}
      lead={offer === 'FEE_WAIVER' && months !== null ? <>A <Term term="waiver">fee waiver</Term> of up to {months} {months === 1 ? 'month' : 'months'} is allowed</> : offer ? `Offer: ${offer}` : 'No offer on record'}>
      {str(pricing.band.segment) && <p className="text-small text-muted">Shop type: <b className="text-fg">{str(pricing.band.segment)}</b></p>}
      <p className="text-small text-muted">Waivers given so far: <b className="text-num text-fg">{pricing.waivers.length}</b></p>
    </Tile>
  )
}

function ListCard({ id, title, data, count, noun, cause, children }: { id: TileId; title: string; data: unknown; count: number; noun: string; cause?: TileCause; children?: ReactNode }) {
  return <Tile id={id} title={title} data={data} cause={cause} lead={count === 0 ? `No ${noun}s yet` : `${count} ${noun}${count === 1 ? '' : 's'}`}>{children}</Tile>
}

export function Systems() {
  const { api, recent } = useLive()
  const merchants = useResource(() => api.cases(), [api])
  const ids = [...new Set((merchants.data?.cases ?? []).map((c) => c.merchant_id))].sort()
  const [chosen, setChosen] = useState<string | null>(null)
  const mid = chosen ?? ids[0] ?? null
  const { data, error } = useResource(() => (mid ? api.systems(mid) : Promise.reject(new Error('no merchant'))), [api, mid], () => true)
  const causes = causesByTile(recent)
  const tickets = (data?.tickets ?? []) as Rec[]
  const comms = (data?.comms ?? []) as Rec[]
  const followUps = data?.scheduler.follow_ups.length ?? 0
  const bookings = data?.scheduler.bookings.length ?? 0
  return (
    <Page eyebrow="Systems" title="The shop floor, right now." intro="Munim remembers nothing on its own. It reads these mock Paytm systems and changes them only through approved actions. Each card says what the data means; the raw record is one click away.">
      <label className="text-small inline-flex items-center gap-2 text-muted">Merchant
        <select className={fieldSm} value={mid ?? ''} onChange={(e) => setChosen(e.target.value)}>{ids.map((i) => <option key={i}>{i}</option>)}</select>
      </label>
      <div className="mt-6">
        {mid === null ? <p className="text-muted">No merchants yet. Open Judge Mode and send a message.</p> : error && !data ? <Note error={error} /> : !data ? loading : (
          <div key={mid} className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <SoundboxCard device={rec(data.device)} cause={causes.device} />
            <MoneyCard payments={{ latest: data.payments.latest.map(rec), settlement: data.payments.settlement ? rec(data.payments.settlement) : null }} cause={causes.payments} />
            <CheckCard id="kyc" title="Identity (KYC)" data={rec(data.kyc)} table={KYC_MOOD} cause={causes.kyc} />
            <CheckCard id="bank" title="Bank account" data={rec(data.bank)} table={BANK_MOOD} cause={causes.bank}>
              {str(data.bank.account_last4) && <p className="text-small text-muted">Account ending <b className="text-num text-fg">{str(data.bank.account_last4)}</b>{str(data.bank.ifsc) && <> · <Term term="IFSC">IFSC</Term> <span className="text-num">{str(data.bank.ifsc)}</span></>}</p>}
            </CheckCard>
            <CasesCard crm={data.crm} cause={causes.crm} />
            <PricingCard pricing={data.pricing} cause={causes.pricing} />
            <Tile id="handoff" title="Waiting for a person" data={data.handoff_queue} cause={causes.handoff}
              lead={<><Counter value={data.handoff_queue.open} className="text-h1 mr-2" /><span className="text-small text-muted">{data.handoff_queue.open === 1 ? 'case is' : 'cases are'} waiting for a person to take over</span></>} />
            <Tile id="scheduler" title="Follow-ups and bookings" data={data.scheduler} cause={causes.scheduler}
              lead={followUps + bookings === 0 ? 'Nothing scheduled' : `${followUps} follow-up${followUps === 1 ? '' : 's'}, ${bookings} booking${bookings === 1 ? '' : 's'}`} />
            <ListCard id="tickets" title="Support tickets" data={data.tickets} count={tickets.length} noun="ticket" cause={causes.tickets}>
              {tickets.map((t, i) => (
                <p key={str(t.ticket_id) ?? i} className="text-small rule-b pb-2">
                  <b className="text-num">{str(t.ticket_id)}</b> · {str(t.type)?.toLowerCase()} · <span className="text-muted">{str(t.status)?.toLowerCase()}</span>
                  {str(t.summary) && <><br />{str(t.summary)}</>}
                </p>
              ))}
            </ListCard>
            <ListCard id="comms" title="Messages sent to the merchant" data={data.comms} count={comms.length} noun="message" cause={causes.comms}>
              {comms.slice(0, 5).map((c, i) => (
                <p key={str(c.comm_id) ?? i} className="text-small rule-b pb-2">
                  <span lang={str(c.language)?.slice(0, 2) ?? undefined}>{str(c.body)}</span>
                  <br /><span className="text-muted">{str(c.channel)} · {LANGUAGE_NAMES[str(c.language) ?? ''] ?? str(c.language)} · {str(c.status)?.toLowerCase()}</span>
                </p>
              ))}
            </ListCard>
          </div>
        )}
      </div>
    </Page>
  )
}
