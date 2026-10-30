import { useState } from 'react'
import { useLive, useResource } from '../hooks/live'
import { ApprovalRefusal } from '../components/ApprovalRefusal'
import { btn, btnDanger, btnPrimary, ErrorNote, inputCls, Loading, Panel, SimulatedBadge } from '../components/common'
import type { ChaosSetting } from '../api/types'

export const CHAOS_SYSTEMS = ['kyc', 'bank', 'device', 'payments', 'crm', 'pricing', 'scheduler', 'ticketing', 'comms']
export const CHAOS_MODES = ['none', 'timeout', 'error', 'slow', 'duplicate']

function CostMeter() {
  const { api } = useLive()
  const cases = useResource(() => api.cases(), [api])
  const [cid, setCid] = useState('')
  const id = cid || cases.data?.cases[0]?.case_id || ''
  const cost = useResource(() => (id ? api.cost(id) : Promise.reject(new Error('no case'))), [api, id], (e) => e.case_id === id)
  const d = cost.data
  return (
    <Panel title="Cost meter (per case)">
      <label className="mb-2 flex items-center gap-2">Case
        <select className={inputCls} value={id} onChange={(e) => setCid(e.target.value)}>
          {(cases.data?.cases ?? []).map((c) => <option key={c.case_id}>{c.case_id}</option>)}
        </select>
      </label>
      {d ? (
        <div data-testid="cost">
          <p className="text-3xl font-bold">AI: INR {d.ai_inr.toFixed(2)}</p>
          <p>Human baseline: low INR {d.human_baseline_inr.low} | mid INR {d.human_baseline_inr.mid} | high INR {d.human_baseline_inr.high}</p>
          <p className="text-sm">Tokens in {d.usage.tokens_in} | out {d.usage.tokens_out}</p>
          <h3 className="mt-2 font-semibold">Assumptions</h3>
          <ul className="list-disc pl-6 text-sm">{d.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      ) : <ErrorNote error={cost.error} />}
    </Panel>
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
    try {
      await api.chaos({ system: sys, mode, rate, param: current?.param ?? null })
      setSaved(true)
    } catch (e) {
      setErr(e)
    }
  }
  return (
    <tr>
      <th scope="row" className="pr-3 text-left">{sys}</th>
      <td><select aria-label={`${sys} chaos mode`} className={inputCls} value={mode} onChange={(e) => { setMode(e.target.value); setSaved(false) }}>{CHAOS_MODES.map((m) => <option key={m}>{m}</option>)}</select></td>
      <td><input aria-label={`${sys} chaos rate`} type="number" min={0} max={1} step={0.1} className={`${inputCls} w-20`} value={rate} onChange={(e) => { setRate(Number(e.target.value)); setSaved(false) }} /></td>
      <td><button type="button" className={btn} onClick={apply}>Apply</button> {saved && <span role="status">applied</span>}<ApprovalRefusal error={err} /></td>
    </tr>
  )
}

export function Control() {
  const { api, setSimTime, simTime } = useLive()
  const ctl = useResource(() => api.control(), [api], (e) => e.kind === 'POLICY' || e.kind === 'STATE')
  const [confirm, setConfirm] = useState(false)
  const [err, setErr] = useState<unknown>(null)
  const [payMid, setPayMid] = useState('')
  const [payRs, setPayRs] = useState('')
  const [payRes, setPayRes] = useState<string | null>(null)
  if (ctl.error && !ctl.data) return <ErrorNote error={ctl.error} />
  if (!ctl.data) return <Loading />
  const c = ctl.data
  const flip = async () => {
    setErr(null)
    try {
      await api.killSwitch(!c.kill_switch)
      setConfirm(false)
      ctl.reload()
    } catch (e) {
      setErr(e)
    }
  }
  const advance = async (h: number) => {
    setErr(null)
    try {
      const r = await api.advanceClock(h)
      setSimTime(r.sim_time)
      ctl.reload()
    } catch (e) {
      setErr(e)
    }
  }
  const pay = async () => {
    setErr(null)
    const paise = Math.round(Number(payRs) * 100)
    if (!payMid || !Number.isFinite(paise) || paise <= 0) return setPayRes('Enter a merchant id and a positive rupee amount')
    try {
      const r = await api.pay(payMid, paise)
      setPayRes(`${r.payment_id}: ${r.status} (settlement ${r.settlement_id})`)
    } catch (e) {
      setErr(e)
    }
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3"><h1 className="text-2xl font-bold">Control panel</h1><SimulatedBadge /></div>
      <ApprovalRefusal error={err} />
      <Panel title="Kill switch">
        <p className="mb-2 text-xl" role="status">Kill switch is <b className={c.kill_switch ? 'text-red-700' : 'text-emerald-700'}>{c.kill_switch ? 'ON (all writes halted)' : 'OFF'}</b></p>
        {!confirm ? (
          <button type="button" className={c.kill_switch ? btn : btnDanger} onClick={() => setConfirm(true)}>{c.kill_switch ? 'Turn kill switch OFF' : 'Turn kill switch ON'}</button>
        ) : (
          <div role="alertdialog" aria-label="Confirm kill switch" className="flex items-center gap-2">
            <span>{c.kill_switch ? 'Resume all writes?' : 'Halt ALL writes and queued sends now?'}</span>
            <button type="button" className={btnPrimary} onClick={flip}>Confirm</button>
            <button type="button" className={btn} onClick={() => setConfirm(false)}>Cancel</button>
          </div>
        )}
      </Panel>
      <Panel title="Simulated clock">
        <p className="mb-2 text-xl">Sim time: <b data-testid="sim-time">{simTime ?? c.sim_time}</b></p>
        <div className="flex gap-2">{[1, 24, 48].map((h) => <button key={h} type="button" className={btn} onClick={() => advance(h)}>+{h}h</button>)}</div>
      </Panel>
      <Panel title="Chaos (per system and mode)">
        <table><tbody>{CHAOS_SYSTEMS.map((s) => <ChaosRow key={s} sys={s} current={c.chaos.find((x) => x.system === s)} />)}</tbody></table>
      </Panel>
      <Panel title="Customer pays">
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col text-sm">Merchant id<input className={inputCls} value={payMid} onChange={(e) => setPayMid(e.target.value)} /></label>
          <label className="flex flex-col text-sm">Amount (INR)<input className={inputCls} inputMode="decimal" value={payRs} onChange={(e) => setPayRs(e.target.value)} /></label>
          <button type="button" className={btnPrimary} onClick={pay}>Customer pays</button>
        </div>
        {payRes && <p role="status" className="mt-2">{payRes}</p>}
      </Panel>
      <CostMeter />
    </div>
  )
}
