import { useState } from 'react'
import { useLive, useResource } from '@old/hooks/live'
import { policyApi, type CompileResult } from '@old/policy/api'
import type { ChaosSetting } from '@old/api/types'
import { Button, Stamp } from '../ui'
import { field, fieldSm, loading, Note, Page, Refusal, Section } from '../parts'

/** The operator's controls: the kill switch, the simulated clock, fault injection per system, a customer payment, the cost meter, and the policy compiler. Every control
 *  goes through the server; a refusal is shown word for word. */

const CHAOS_SYSTEMS = ['kyc', 'bank', 'device', 'payments', 'crm', 'pricing', 'scheduler', 'ticketing', 'comms']
const CHAOS_MODES = ['none', 'timeout', 'error', 'slow', 'duplicate']

function CostMeter() {
  const { api } = useLive()
  const cases = useResource(() => api.cases(), [api])
  const [cid, setCid] = useState('')
  const id = cid || cases.data?.cases[0]?.case_id || ''
  const cost = useResource(() => (id ? api.cost(id) : Promise.reject(new Error('no case'))), [api, id], (e) => e.case_id === id)
  const d = cost.data
  return (
    <Section title="Cost meter" note="What a case cost in AI usage, against what a person would cost for the same work. The assumptions are listed so they can be argued with.">
      <label className="text-small inline-flex items-center gap-2 text-muted">Case
        <select className={fieldSm} value={id} onChange={(e) => setCid(e.target.value)}>{(cases.data?.cases ?? []).map((c) => <option key={c.case_id}>{c.case_id}</option>)}</select>
      </label>
      {d ? (
        <div className="mt-4">
          <p className="text-display text-num">₹{d.ai_inr.toFixed(2)}</p>
          <p className="text-small mt-1 text-muted">AI cost for this case · tokens in {d.usage.tokens_in}, out {d.usage.tokens_out}</p>
          <p className="mt-3">Human baseline: low ₹{d.human_baseline_inr.low} · mid ₹{d.human_baseline_inr.mid} · high ₹{d.human_baseline_inr.high}</p>
          <h3 className="text-label mt-4 text-muted">Assumptions</h3>
          <ul className="text-small mt-1 list-disc pl-5">{d.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      ) : <Note error={cost.error} />}
    </Section>
  )
}

function ChaosRow({ sys, current }: { sys: string; current?: ChaosSetting }) {
  const { api } = useLive()
  const [mode, setMode] = useState(current?.mode ?? 'none')
  const [rate, setRate] = useState(current?.rate ?? 0)
  const [err, setErr] = useState<unknown>(null)
  const [saved, setSaved] = useState(false)
  const apply = async () => {
    setErr(null)
    try { await api.chaos({ system: sys, mode, rate, param: current?.param ?? null }); setSaved(true) } catch (e) { setErr(e) }
  }
  return (
    <tr className="rule-b">
      <th scope="row" className="text-num py-2 pr-4 text-left font-normal">{sys}</th>
      <td className="pr-3"><select aria-label={`${sys} chaos mode`} className={fieldSm} value={mode} onChange={(e) => { setMode(e.target.value); setSaved(false) }}>{CHAOS_MODES.map((m) => <option key={m}>{m}</option>)}</select></td>
      <td className="pr-3"><input aria-label={`${sys} chaos rate`} type="number" min={0} max={1} step={0.1} className={`${fieldSm} w-20`} value={rate} onChange={(e) => { setRate(Number(e.target.value)); setSaved(false) }} /></td>
      <td><Button className="min-h-9 px-3 text-small" onClick={() => void apply()}>Apply</Button> {saved && <span role="status" className="text-small text-ok">applied</span>}<Refusal error={err} /></td>
    </tr>
  )
}

function PolicyCompiler({ role, principalId }: { role: string; principalId: string }) {
  const [text, setText] = useState('')
  const [res, setRes] = useState<CompileResult | null>(null)
  const [msg, setMsg] = useState('')
  const [failure, setFailure] = useState<string | null>(null)
  const draft = res && res.ok ? res.draft : null
  const compile = async () => {
    setMsg(''); setFailure(null)
    try { setRes(await policyApi.compile(text)) } catch (e) { setFailure(e instanceof Error ? e.message : String(e)) }
  }
  return (
    <Section title="Policy compiler" note="Type a rule in plain words. The server compiles it, shows the difference and a dry run, and only an ops_admin can approve. A rule can only tighten what is allowed.">
      <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); if (text.trim()) void compile() }}>
        <input aria-label="Rule in plain words" className={`${field} min-w-72 flex-1`} value={text} onChange={(e) => setText(e.target.value)} placeholder="Block create_device_replacement for support executive" />
        <Button type="submit" variant="primary" disabled={!text.trim()}>Compile</Button>
      </form>
      <Note error={failure} />
      {res && !res.ok && <p role="alert" className="text-bad mt-3">{res.error.error}: {res.error.message}</p>}
      {draft && (
        <div className="mt-5 border border-line bg-surface p-4">
          <p className="font-semibold">{draft.summary} <span className="text-small text-muted">({draft.source})</span></p>
          <pre className="text-num text-small mt-3 overflow-auto whitespace-pre-wrap border border-line bg-bg p-3">{draft.diff.lines.join('\n')}</pre>
          <p className="text-small mt-3" data-ok={draft.ok}>Dry run: {draft.dry_run.probes_run} probes, {draft.dry_run.changed.length} changed, {draft.dry_run.loosened.length} weakened</p>
          <p className="text-small text-muted">Rule {draft.rule_id} · hash <span className="text-num">{draft.rule_hash.slice(0, 12)}</span></p>
          <div className="mt-3 flex gap-2">
            <Button variant="primary" disabled={!draft.ok} onClick={async () => { const r = await policyApi.approve(draft, principalId, role); setMsg(r.ok ? `Approved ${draft.rule_id}` : `${r.error?.error}: ${r.error?.message}`) }}>Approve</Button>
            <Button onClick={async () => { const r = await policyApi.revert(draft.rule_id, principalId, role); setMsg(r.ok ? `Reverted ${draft.rule_id}` : `${r.error?.error}: ${r.error?.message}`) }}>Revert</Button>
          </div>
        </div>
      )}
      {msg && <p role="status" className="text-small mt-3">{msg}</p>}
    </Section>
  )
}

export function Control() {
  const { api, setSimTime, simTime, identity } = useLive()
  const ctl = useResource(() => api.control(), [api], (e) => e.kind === 'POLICY' || e.kind === 'STATE')
  const [confirm, setConfirm] = useState(false)
  const [err, setErr] = useState<unknown>(null)
  const [payMid, setPayMid] = useState('')
  const [payRs, setPayRs] = useState('')
  const [payRes, setPayRes] = useState<string | null>(null)
  if (ctl.error && !ctl.data) return <Page eyebrow="Control" title="Operator controls."><Note error={ctl.error} /></Page>
  if (!ctl.data) return <Page eyebrow="Control" title="Operator controls.">{loading}</Page>
  const c = ctl.data
  const flip = async () => {
    setErr(null)
    try { await api.killSwitch(!c.kill_switch); setConfirm(false); ctl.reload() } catch (e) { setErr(e) }
  }
  const advance = async (h: number) => {
    setErr(null)
    try { const r = await api.advanceClock(h); setSimTime(r.sim_time); ctl.reload() } catch (e) { setErr(e) }
  }
  const pay = async () => {
    setErr(null)
    const paise = Math.round(Number(payRs) * 100)
    if (!payMid || !Number.isFinite(paise) || paise <= 0) return setPayRes('Enter a merchant id and a positive rupee amount')
    try { const r = await api.pay(payMid, paise); setPayRes(`${r.payment_id}: ${r.status} (settlement ${r.settlement_id})`) } catch (e) { setErr(e) }
  }
  return (
    <Page eyebrow="Control" title="Operator controls." intro="Everything here acts on the simulated world only. Each control goes through the server, and the server can say no.">
      <Refusal error={err} />
      <Section title="Kill switch" note="Halts every write and every queued send at once.">
        <p className="mb-3 text-h3" role="status">{c.kill_switch ? <Stamp tone="bad">ON · all writes halted</Stamp> : <Stamp tone="ok">OFF</Stamp>}</p>
        {!confirm ? (
          <Button className={c.kill_switch ? '' : 'text-bad'} onClick={() => setConfirm(true)}>{c.kill_switch ? 'Turn kill switch OFF' : 'Turn kill switch ON'}</Button>
        ) : (
          <div role="alertdialog" aria-label="Confirm kill switch" className="flex flex-wrap items-center gap-3">
            <span>{c.kill_switch ? 'Resume all writes?' : 'Halt ALL writes and queued sends now?'}</span>
            <Button variant="primary" onClick={() => void flip()}>Confirm</Button>
            <Button onClick={() => setConfirm(false)}>Cancel</Button>
          </div>
        )}
      </Section>
      <Section title="Simulated clock" note="Advance time to see waits, undo windows and follow-ups play out.">
        <p className="text-h3 text-num">{simTime ?? c.sim_time}</p>
        <div className="mt-3 flex gap-2">{[1, 24, 48].map((h) => <Button key={h} onClick={() => void advance(h)}>+{h}h</Button>)}</div>
      </Section>
      <Section title="Chaos" note="Make one system slow, fail or answer twice, to watch Munim cope.">
        <div className="overflow-x-auto"><table className="min-w-[36rem]"><tbody>{CHAOS_SYSTEMS.map((s) => <ChaosRow key={s} sys={s} current={c.chaos.find((x) => x.system === s)} />)}</tbody></table></div>
      </Section>
      <Section title="Customer pays" note="Simulate a payment arriving for a merchant. The amount is entered in rupees and sent as integer paise.">
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-small flex flex-col gap-1 text-muted">Merchant id<input className={field} value={payMid} onChange={(e) => setPayMid(e.target.value)} /></label>
          <label className="text-small flex flex-col gap-1 text-muted">Amount (INR)<input className={field} inputMode="decimal" value={payRs} onChange={(e) => setPayRs(e.target.value)} /></label>
          <Button variant="primary" onClick={() => void pay()}>Customer pays</Button>
        </div>
        {payRes && <p role="status" className="mt-3">{payRes}</p>}
      </Section>
      <CostMeter />
      <PolicyCompiler role={identity.role} principalId={identity.principal_id ?? 'u-ops'} />
    </Page>
  )
}
