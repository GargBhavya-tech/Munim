import { useMemo, useState, type ReactNode } from 'react'
import { useResource } from '@old/hooks/live'
import { CHANNEL_LABEL, frame, getLead, getLeads, inr, nice, STATE_LABEL, type Bounds, type Channel, type Contact, type Funnel, type Lead, type LeadDetail, type LeadsView } from '../leads'
import { Mark, Term } from '../plain'
import { fieldSm, loading, Note, Page } from '../parts'
import { Readout, Stamp, Tag } from '../ui'

/** New merchants: how Munim narrows public data to the few businesses worth reaching, and why. It REPORTS what the jobs decided (found, dropped as existing merchants or
 *  duplicates, ranked) and shows each lead's brief, whose every number cites a source. Contact is shown, never started here. All data is synthetic. */

const SHOWN = 25
const MAP_W = 640
const CHANNEL_TONE: Record<Channel, string> = { field_rep: 'var(--stamp)', ai_voice: 'var(--ink)', self_serve: 'var(--muted)' }

// ---------------------------------------------------------------------------------------------------- the funnel
function FunnelStrip({ f }: { f: Funnel }) {
  return (
    <>
      <ol aria-label="How the leads were narrowed down" className="grid gap-x-8 gap-y-6 sm:grid-cols-2 lg:grid-cols-5">
        <li><Readout label="Found" value={f.found} note="Every lead the discovery job collected" tone="ink" /></li>
        <li><Readout label="Already merchants" value={f.existing_merchant} note="Left out: they are Paytm customers already" tone="muted" /></li>
        <li><Readout label="Possible matches" value={f.needs_review} note="Left out until a person decides" tone="warn" /></li>
        <li><Readout label="Duplicates" value={f.duplicate} note="Left out: a better lead covers them" tone="muted" /></li>
        <li><Readout label="Ready to pitch" value={f.cleared} note="Ranked below, best first" tone="stamp" /></li>
      </ol>
      {f.not_checked > 0 && <p className="text-small mt-3 text-muted">{f.not_checked} leads have not been checked for duplicates yet, so they are not shown.</p>}
    </>
  )
}

// ---------------------------------------------------------------------------------------------------- the map
function Marker({ x, y, r, channel }: { x: number; y: number; r: number; channel: Channel }) {
  const fill = CHANNEL_TONE[channel]
  if (channel === 'field_rep') return <rect x={x - r} y={y - r} width={2 * r} height={2 * r} fill={fill} />
  if (channel === 'self_serve') return <polygon points={`${x},${y - r * 1.25} ${x + r * 1.25},${y} ${x},${y + r * 1.25} ${x - r * 1.25},${y}`} fill={fill} />
  return <circle cx={x} cy={y} r={r} fill={fill} />
}

function LeadMap({ leads, bounds, picked, onPick }: { leads: Lead[]; bounds: Bounds; picked: string | null; onPick: (id: string) => void }) {
  const fr = useMemo(() => frame(bounds, MAP_W), [bounds])
  const top = Math.max(1, ...leads.map((l) => l.score ?? 0))
  const placed = leads.filter((l) => l.lat !== null && l.lng !== null && l.channel)
  const chosen = placed.find((l) => l.lead_id === picked)
  const chosenAt = chosen ? fr.at(chosen.lat as number, chosen.lng as number) : null
  return (
    <figure className="m-0">
      <svg viewBox={`0 0 ${fr.w} ${fr.h}`} role="img" aria-label={`Map of ${placed.length} leads by position, north up. The ranked list has the same leads.`}
        className="block w-full border border-line bg-surface" preserveAspectRatio="xMidYMid meet">
        {placed.filter((l) => l.lead_id !== picked).map((l) => {
          const [x, y] = fr.at(l.lat as number, l.lng as number)
          return (
            <g key={l.lead_id} onClick={() => onPick(l.lead_id)} className="cursor-pointer" opacity="0.8">
              <title>{l.name}</title>
              <Marker x={x} y={y} r={3 + 4 * ((l.score ?? 0) / top)} channel={l.channel as Channel} />
            </g>
          )
        })}
        {chosen && chosenAt && (
          <g>
            <circle cx={chosenAt[0]} cy={chosenAt[1]} r={13} fill="none" stroke="var(--fg)" strokeWidth="2" />
            <Marker x={chosenAt[0]} y={chosenAt[1]} r={5 + 4 * ((chosen.score ?? 0) / top)} channel={chosen.channel as Channel} />
          </g>
        )}
      </svg>
      <figcaption className="text-small mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-muted">
        {(['field_rep', 'ai_voice', 'self_serve'] as Channel[]).map((c) => (
          <span key={c} className="inline-flex items-center gap-2">
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><Marker x={7} y={7} r={5} channel={c} /></svg>{CHANNEL_LABEL[c]}
          </span>
        ))}
        <span>Bigger means a higher <Term term="priority score">priority</Term>. North is up. Positions are synthetic.</span>
      </figcaption>
    </figure>
  )
}

// ---------------------------------------------------------------------------------------------------- the ranked list
function Row({ lead, rank, picked, onPick }: { lead: Lead; rank: number; picked: boolean; onPick: () => void }) {
  return (
    <li>
      <button type="button" aria-pressed={picked} onClick={onPick}
        className={`rule-b grid min-h-14 w-full grid-cols-[2.25rem_1fr_auto] items-baseline gap-x-3 px-2 py-2.5 text-left transition-colors duration-150 ${picked ? 'bg-surface outline outline-1 outline-fg' : 'hover:bg-surface'}`}>
        <span className="text-num text-muted">{rank}</span>
        <span className="min-w-0">
          <span className="font-semibold">{lead.name}</span>
          <span className="text-small text-muted"> · {nice(lead.category)} · {lead.pincode ?? 'no pincode'}</span>
          <span className="text-small block text-muted">
            {lead.channel ? CHANNEL_LABEL[lead.channel] : 'not ranked'} · about {inr(lead.monthly_rupees)} a month{lead.uncertain ? ' (a wide, uncertain band)' : ''}
            {lead.do_not_contact && <> · <span className="text-bad"><Term term="do not disturb">do not disturb</Term></span></>}
          </span>
        </span>
        <span className="text-num text-small" aria-label={`priority score ${lead.score ?? 'none'}`}>{lead.score ?? '–'}</span>
      </button>
    </li>
  )
}

// ---------------------------------------------------------------------------------------------------- the brief
const Source = ({ id }: { id: string }) => <span className="text-num text-small break-all text-muted">[{id}]</span>

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-5">
      <h3 className="text-label text-ink">{title}</h3>
      <div className="mt-2 space-y-2 text-small">{children}</div>
    </section>
  )
}

function ContactBlock({ c }: { c: Contact }) {
  const word = (s: string) => CHANNEL_LABEL[s] ?? s
  return (
    <Block title="Can we contact them?">
      <p><Mark mood={c.can_contact_now ? 'ok' : 'wait'}>{c.can_contact_now ? `Yes, by ${word(c.chosen_channel ?? '')}` : 'Not yet'}</Mark></p>
      {c.why_not && <p className="text-muted">{c.why_not}.</p>}
      <p className="text-muted">
        Would be reached by <b className="text-fg">{c.order.map(word).join(', then ')}</b>. <Term term="consent">Consent</Term> on record:{' '}
        <b className="text-fg">{c.consented_channels.length ? c.consented_channels.map(word).join(', ') : 'none'}</b>.{' '}
        <Term term="do not disturb">Do not disturb</Term>: <b className="text-fg">{c.do_not_contact ? 'yes' : 'no'}</b>.
      </p>
    </Block>
  )
}

function BriefBody({ d }: { d: LeadDetail }) {
  const b = d.brief
  const l = d.lead
  const fields = Object.entries(d.provenance.fields)
  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <div className="text-label text-muted">Account brief · {l.lead_id}</div>
          <h2 className="text-h2 mt-1">{l.name}</h2>
        </div>
        {b.passes_gate && <Stamp tone="ok">every number sourced</Stamp>}
      </div>
      <p className="text-small mt-2 text-muted">
        <Term term="priority score">Priority</Term> <b className="text-num text-fg">{l.score ?? 'none'}</b>
        {l.channel && <> · reached by <b className="text-fg">{CHANNEL_LABEL[l.channel]}</b></>}
      </p>

      <Block title="Who they are">
        <ul className="space-y-2">{b.who.map((c) => <li key={c.id}>{c.text} <Source id={c.source} /></li>)}</ul>
      </Block>
      <Block title="What they use now">{b.uses_now.map((t) => <p key={t}>{t}</p>)}</Block>
      <Block title="What they probably need (a hypothesis)">{b.needs.length ? b.needs.map((t) => <p key={t}>{t}</p>) : <p className="text-muted">No hypothesis for this kind of business.</p>}</Block>
      <Block title="What to pitch">
        <ul className="space-y-2">
          {b.pitch.map((p) => <li key={p.id}><b>{p.rank}: {p.label}.</b> {p.text} <Source id={p.source} /></li>)}
        </ul>
      </Block>
      <Block title="Likely objections, with the approved answer">
        <ul className="space-y-3">
          {b.objections.map((o) => <li key={o.key}><span className="text-label text-muted">{nice(o.key)}</span><br /><span className="text-voice">{o.text}</span> <Source id={o.source} /></li>)}
        </ul>
      </Block>
      <ContactBlock c={d.contact} />
      {b.contact.map((c) => <p key={c.id} className="text-small mt-2 text-muted">{c.text} <Source id={c.source} /></p>)}
      {b.contact_notes.map((t) => <p key={t} className="text-small mt-2 text-muted">{t}</p>)}
      {b.gaps.length > 0 && (
        <Block title="Not known: the data does not hold it">
          <ul className="list-disc space-y-1 pl-5 text-muted">{b.gaps.map((g) => <li key={g}>{g}</li>)}</ul>
        </Block>
      )}
      <Block title="Where this record came from">
        <ul className="space-y-1 text-muted">
          {fields.map(([k, v]) => <li key={k}><b className="text-fg">{nice(k)}</b>: {v.source}, confidence {Math.round(v.confidence * 100)}%, fetched {v.fetched_at.slice(0, 10)}</li>)}
          <li><b className="text-fg">Duplicate check</b>: {String(d.provenance.dedupe.result ?? 'not run')}</li>
        </ul>
      </Block>
    </div>
  )
}

function BriefPanel({ id }: { id: string }) {
  const r = useResource(() => getLead(id), [id])
  if (r.error) return <p role="alert" className="text-bad">Could not load this brief: {r.error}</p>
  if (!r.data) return loading
  if (!r.data.ok) {
    return (
      <div>
        <Stamp tone="warn">no brief</Stamp>
        <p className="text-body mt-3">{r.data.reason}</p>
        {r.data.matchedMerchant && <p className="text-small mt-2 text-muted">Matches the existing merchant <b className="text-num text-fg">{r.data.matchedMerchant}</b>.</p>}
      </div>
    )
  }
  return <BriefBody d={r.data.detail} />
}

// ---------------------------------------------------------------------------------------------------- left out
function LeftOut({ view }: { view: LeadsView }) {
  const total = view.funnel.existing_merchant + view.funnel.needs_review + view.funnel.duplicate
  return (
    <details className="mt-8 border border-line p-4">
      <summary className="text-label min-h-9 cursor-pointer text-muted hover:text-fg">Left out of the list: {total} leads, and why</summary>
      {view.excluded.length < total && <p className="text-small mt-3 text-muted">Showing the first {view.excluded.length}.</p>}
      <ul className="mt-3">
        {view.excluded.map((e) => (
          <li key={e.lead_id} className="rule-b text-small py-2">
            <b>{e.name}</b> <span className="text-num text-muted">{e.lead_id}</span> · {STATE_LABEL[e.state] ?? nice(e.state)}
            {(e.matched_merchant_id || e.duplicate_of) && <> · <span className="text-num">{e.matched_merchant_id ?? e.duplicate_of}</span></>}
            <br /><span className="text-muted">{e.reason}</span>
          </li>
        ))}
      </ul>
    </details>
  )
}

// ---------------------------------------------------------------------------------------------------- the screen
export function Leads() {
  const view = useResource(getLeads, [])
  const [picked, setPicked] = useState<string | null>(null)
  const [channel, setChannel] = useState<string>('all')
  const [category, setCategory] = useState<string>('all')
  const [all, setAll] = useState(false)
  const data = view.data
  const shown = useMemo(() => (data?.leads ?? []).filter((l) => (channel === 'all' || l.channel === channel) && (category === 'all' || l.category === category)), [data, channel, category])
  const categories = useMemo(() => [...new Set((data?.leads ?? []).map((l) => l.category).filter((c): c is string => !!c))].sort(), [data])
  const current = picked && shown.some((l) => l.lead_id === picked) ? picked : shown[0]?.lead_id ?? null
  return (
    <Page eyebrow="New merchants" title="Who to reach next, and why."
      intro="Munim collects businesses that are not Paytm merchants yet, drops the ones that already are, ranks the rest by expected value, and writes each a short brief. All of this is synthetic, and the ranking uses assumed rates, not measured ones.">
      {view.error && !data ? <Note error={view.error} /> : !data ? loading : (
        <>
          <FunnelStrip f={data.funnel} />
          {data.leads.length === 0 ? (
            <p className="text-body mt-10 text-muted">No lead has passed the duplicate check yet, so there is nothing to rank. The duplicate-matching job has to run first.</p>
          ) : (
            <div className="mt-10 grid gap-8 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
              <div className="min-w-0">
                {data.bounds && <LeadMap leads={shown} bounds={data.bounds} picked={current} onPick={setPicked} />}
                <div className="mt-6 flex flex-wrap items-center gap-4">
                  <label className="text-small inline-flex items-center gap-2 text-muted">Reached by
                    <select className={fieldSm} value={channel} onChange={(e) => setChannel(e.target.value)}>
                      <option value="all">anyone</option>
                      {(['field_rep', 'ai_voice', 'self_serve'] as Channel[]).map((c) => <option key={c} value={c}>{CHANNEL_LABEL[c]}</option>)}
                    </select>
                  </label>
                  <label className="text-small inline-flex items-center gap-2 text-muted">Kind of business
                    <select className={fieldSm} value={category} onChange={(e) => setCategory(e.target.value)}>
                      <option value="all">all</option>
                      {categories.map((c) => <option key={c} value={c}>{nice(c)}</option>)}
                    </select>
                  </label>
                  <Tag>{shown.length} of {data.leads.length}</Tag>
                </div>
                <h2 className="text-label mt-6 text-muted">Ranked by <Term term="priority score">priority</Term>, best first</h2>
                {shown.length === 0 ? <p className="text-small mt-3 text-muted">No lead matches these filters.</p> : (
                  <ol className="mt-2">
                    {(all ? shown : shown.slice(0, SHOWN)).map((l, i) => <Row key={l.lead_id} lead={l} rank={i + 1} picked={l.lead_id === current} onPick={() => setPicked(l.lead_id)} />)}
                  </ol>
                )}
                {shown.length > SHOWN && (
                  <button type="button" onClick={() => setAll(!all)} className="text-label mt-3 min-h-10 border border-line px-4 text-muted hover:text-fg">
                    {all ? `Show the top ${SHOWN} only` : `Show all ${shown.length}`}
                  </button>
                )}
              </div>
              <aside aria-label="Account brief" className="min-w-0 border border-line bg-surface p-5 lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:self-start lg:overflow-auto">
                {current ? <BriefPanel key={current} id={current} /> : <p className="text-muted">Pick a lead to read its brief.</p>}
              </aside>
            </div>
          )}
          <LeftOut view={data} />
        </>
      )}
    </Page>
  )
}
