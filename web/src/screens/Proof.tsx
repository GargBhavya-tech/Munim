import { useEffect, useMemo, useRef, useState } from 'react'
import { useLive } from '@old/hooks/live'
import type { LedgerEntry as Row, ReplayResponse, VerifyResult } from '@old/api/types'
import { WhyDrawer } from '../chat'
import { summarise, toneOf } from '../ledger'
import { Button, LedgerEntry, Reveal, Stamp, Sweep, Tag } from '../ui'

/** The Proof Ledger: the hash-chained book of everything Munim did. New entries turn in as they happen; "Verify chain" walks the whole chain and an ink line passes
 *  down it; a refusal carries its rule and the way to ask why. Questions to the ledger are fixed read-only templates, for the auditor role only. */

interface AskAnswer { matched: string | null; title?: string; rows: { seq: number; case_id: string | null; ts_sim: string; kind: string; summary: string }[]; needs?: string[]; can_ask?: { id: string; title: string }[]; count?: number; truncated?: boolean }

const merge = (a: Row[], b: Row[]): Row[] => {
  const m = new Map<number, Row>()
  for (const e of [...a, ...b]) m.set(e.seq, e)
  return [...m.values()].sort((x, y) => x.seq - y.seq)
}

export function Proof() {
  const { api, identity, recent } = useLive()
  const [rows, setRows] = useState<Row[]>([])
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [kinds, setKinds] = useState<Set<string>>(new Set())
  const [caseF, setCaseF] = useState('')
  const [open, setOpen] = useState<number | null>(null)
  const [why, setWhy] = useState<number | null>(null)
  const [sweep, setSweep] = useState(0)
  const [verdict, setVerdict] = useState<VerifyResult | null>(null)
  const [verifyErr, setVerifyErr] = useState<string | null>(null)
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState<AskAnswer | null>(null)
  const [askErr, setAskErr] = useState<string | null>(null)
  const [freshFrom, setFreshFrom] = useState<number | null>(null)
  const [replay, setReplay] = useState<ReplayResponse | null>(null)
  const [replayErr, setReplayErr] = useState<string | null>(null)
  const loaded = useRef(false)

  useEffect(() => {
    api.verify().then((v) => api.ledger(Math.max(0, v.entries - 60), 100)).then((p) => {
      setRows((r) => merge(p.entries, r))
      loaded.current = true
      setFreshFrom(p.entries.length ? p.entries[p.entries.length - 1].seq : 0)           // only entries after this one turn in
    }).catch((e: unknown) => setLoadErr(e instanceof Error ? e.message : String(e)))
  }, [api])

  useEffect(() => { if (recent.length) setRows((r) => merge(r, recent)) }, [recent])

  const verify = () => {
    setVerifyErr(null); setSweep((n) => n + 1)
    api.verify().then(setVerdict, (e: unknown) => setVerifyErr(e instanceof Error ? e.message : String(e)))
  }

  const doReplay = () => {
    if (!caseF) return
    setReplayErr(null)
    api.replay(caseF).then(setReplay, (e: unknown) => setReplayErr(e instanceof Error ? e.message : String(e)))
  }

  const ask = async (q: string) => {
    setAskErr(null); setAnswer(null)
    const res = await fetch('/api/ledger/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: q, role: identity.role }) })
    const body = await res.json().catch(() => ({}))
    if (res.status === 403) return setAskErr(`Only the auditor role may question the ledger. You are acting as ${identity.role}: change "Acting as" in the header.`)
    if (res.status === 422) return setAskErr(body?.detail?.error === 'SQL_NOT_ACCEPTED' ? 'That looks like SQL. Nothing was run. Ask in plain words.' : 'That question could not be used.')
    if (!res.ok) return setAskErr(`The ledger could not answer (HTTP ${res.status}).`)
    setAnswer(body as AskAnswer)
  }

  const allKinds = useMemo(() => [...new Set(rows.map((r) => r.kind))].sort(), [rows])
  const cases = useMemo(() => [...new Set(rows.map((r) => r.case_id).filter((c): c is string => !!c))].sort(), [rows])
  const shown = rows.filter((r) => (kinds.size === 0 || kinds.has(r.kind)) && (!caseF || r.case_id === caseF))

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <div className="text-label text-muted">Proof Ledger</div>
      <h1 className="text-h1 mt-1">Every action, in a book you can verify.</h1>
      <p className="text-body text-muted mt-4 max-w-[62ch]">Each entry is chained to the one before it by a hash. Change any entry and every hash after it breaks, so the first break shows exactly where the book was touched.</p>

      <div className="mt-8 flex flex-wrap items-center gap-4">
        <Button variant="primary" onClick={verify}>Verify chain</Button>
        {verdict && (verdict.ok ? <Stamp tone="ok">chain intact · {verdict.entries} entries</Stamp> : <Stamp tone="bad">first break at entry {verdict.first_mismatch}</Stamp>)}
        {verifyErr && <span role="alert" className="text-bad text-small">Could not verify: {verifyErr}</span>}
        <Tag>live</Tag>
        {caseF && <Button onClick={doReplay}>Replay {caseF}</Button>}
        {caseF && <a className="text-label text-ink underline-offset-4 hover:underline" href={`/api/cases/${encodeURIComponent(caseF)}/evidence`} target="_blank" rel="noreferrer">Evidence pack for {caseF}</a>}
      </div>
      {replayErr && <p role="alert" className="text-bad text-small mt-3">Could not replay: {replayErr}</p>}
      {replay && (
        <section aria-label="Replay" className="mt-5 border border-line bg-surface p-5">
          <h2 className="text-h3">Replay of {replay.case_id}, from the ledger alone</h2>
          <p className="mt-2 flex flex-wrap items-center gap-3 text-small">
            {replay.chain.ok ? <Stamp tone="ok">chain intact</Stamp> : <Stamp tone="bad">chain broken at {replay.chain.first_mismatch}</Stamp>}
            <span className="text-muted">state hash <code className="text-num">{replay.state_hash.slice(0, 16)}</code></span>
          </p>
          <ol className="mt-3 space-y-1">{replay.steps.map((st) => <li key={st.seq} className="text-small"><span className="text-num text-muted">#{st.seq}</span> {st.summary}</li>)}</ol>
        </section>
      )}

      <div className="mt-6 flex flex-wrap items-center gap-2" role="group" aria-label="Filter by kind">
        {allKinds.map((k) => (
          <button key={k} type="button" aria-pressed={kinds.has(k)} onClick={() => setKinds((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n })}
            className={`text-label min-h-9 rounded-[2px] border px-2 ${kinds.has(k) ? 'border-fg bg-fg text-bg' : 'border-line text-muted hover:text-fg'}`}>{k}</button>
        ))}
        <select aria-label="Filter by case" value={caseF} onChange={(e) => setCaseF(e.target.value)} className="min-h-9 rounded-[2px] border border-line bg-surface px-2 text-fg">
          <option value="">All cases</option>
          {cases.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>

      <Sweep run={sweep} className="mt-5 border border-line bg-surface">
        {loadErr && <p role="alert" className="p-5 text-bad">The ledger could not be read: {loadErr}. Is the backend running on port 8000?</p>}
        {!loadErr && loaded.current && shown.length === 0 && <p className="p-5 text-muted">{rows.length === 0 ? 'The ledger is empty. Send a message in Judge Mode and the first entry will turn in here.' : 'No entries match these filters.'}</p>}
        <ol>
          {shown.map((e, i) => {
            const refused = e.kind === 'POLICY' && (e.payload as Record<string, unknown>).allow === false
            return (
              <LedgerEntry key={e.seq} seq={e.seq} kind={e.kind} actor={e.actor} summary={summarise(e)} hash={e.hash} first={i === 0} tone={toneOf(e)} fresh={freshFrom !== null && e.seq > freshFrom}>
                <span className="mt-1 block">
                  <button type="button" aria-expanded={open === e.seq} onClick={() => setOpen(open === e.seq ? null : e.seq)} className="text-label text-ink underline-offset-4 hover:underline">{open === e.seq ? 'hide details' : 'details'}</button>
                  {refused && <button type="button" onClick={() => setWhy(e.seq)} className="text-label ml-4 text-stamp underline-offset-4 hover:underline">Kyun nahi?</button>}
                  {open === e.seq && <pre className="text-num text-small mt-2 max-h-64 overflow-auto whitespace-pre-wrap border border-line bg-bg p-3">{JSON.stringify({ case_id: e.case_id, ts_sim: e.ts_sim, role: e.role, model_id: e.model_id, policy_hash: e.policy_hash, hash: e.hash, payload: e.payload }, null, 2)}</pre>}
                </span>
              </LedgerEntry>
            )
          })}
        </ol>
      </Sweep>

      <Reveal>
        <section className="rule-t mt-14 pt-8" aria-label="Questions to the ledger">
          <h2 className="text-h2">Ask the ledger</h2>
          <p className="text-body text-muted mt-2 max-w-[60ch]">Plain questions only, such as “every refusal today” or “every escalation”. Each one maps to a fixed read-only query; nothing you type is ever run as SQL.</p>
          <form className="mt-5 flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); if (question.trim()) void ask(question) }}>
            <input aria-label="Question" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Every refused action today" className="min-h-11 min-w-72 flex-1 rounded-[4px] border border-line bg-surface px-3 text-fg placeholder:text-muted" />
            <Button type="submit" variant="primary" disabled={!question.trim()}>Ask</Button>
          </form>
          {identity.role !== 'auditor' && <p className="text-small text-muted mt-2">You are acting as {identity.role}. This box answers only for the auditor role.</p>}
          {askErr && <p role="alert" className="text-bad mt-4">{askErr}</p>}
          {answer && (
            <div className="mt-6">
              {answer.matched === null && (
                <div><p className="text-body">That is not a question the ledger can answer. These are:</p>
                  <div className="mt-3 flex flex-wrap gap-2">{answer.can_ask?.map((c) => <Button key={c.id} className="min-h-9 px-3 text-small" onClick={() => { setQuestion(c.title); void ask(c.title) }}>{c.title}</Button>)}</div></div>
              )}
              {answer.matched !== null && answer.needs?.length ? <p className="text-body">That question needs: {answer.needs.join(', ')}.</p> : null}
              {answer.matched !== null && !answer.needs?.length && (
                <div>
                  <div className="flex items-center gap-3"><h3 className="text-h3">{answer.title}</h3><Tag>{answer.count ?? answer.rows.length} found{answer.truncated ? ' (first page)' : ''}</Tag></div>
                  {answer.rows.length === 0 && <p className="text-muted mt-2">Nothing matches. That is an answer too: it did not happen.</p>}
                  <ul className="mt-3">{answer.rows.map((r) => <li key={r.seq} className="rule-b grid grid-cols-[4rem_9rem_1fr] gap-x-4 py-2"><span className="text-num text-muted">{r.seq}</span><span className="text-label">{r.kind}</span><span>{r.summary}</span></li>)}</ul>
                </div>
              )}
            </div>
          )}
        </section>
      </Reveal>
      <WhyDrawer api={api} seq={why} onClose={() => setWhy(null)} />
    </div>
  )
}
