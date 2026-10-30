import { useState } from 'react'
import { useLive, useResource } from '../hooks/live'
import { ApprovalRefusal } from '../components/ApprovalRefusal'
import { btn, btnDanger, btnPrimary, ErrorNote, inputCls, Json, Loading, Panel } from '../components/common'
import type { Handoff } from '../api/types'

export const byDeadline = (hs: Handoff[]): Handoff[] => hs.slice().sort((a, b) => Date.parse(a.deadline) - Date.parse(b.deadline))

export function ApprovalDialog({ approvalId, onDecided }: { approvalId: string; onDecided?: () => void }) {
  const { api, identity } = useLive()
  const { data, error, reload } = useResource(() => api.approval(approvalId), [api, approvalId], (e) => e.kind === 'HUMAN_ACTION')
  const [refusal, setRefusal] = useState<unknown>(null)
  const [done, setDone] = useState<string | null>(null)
  if (error && !data) return <ErrorNote error={error} />
  if (!data) return <Loading />
  const decide = async (v: 'approve' | 'reject') => {
    setRefusal(null)
    try {
      const r = await api.decide(approvalId, v, identity)
      setDone(r.status)
      reload()
      onDecided?.()
    } catch (e) {
      setRefusal(e)
    }
  }
  return (
    <div className="flex flex-col gap-3" data-testid="approval-dialog">
      <h3 className="text-lg font-bold">Approval {data.approval_id}: {data.summary}</h3>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
        <dt className="font-semibold">Status</dt><dd>{data.status}</dd>
        <dt className="font-semibold">Risk class</dt><dd>{data.risk_class}</dd>
        <dt className="font-semibold">Case</dt><dd>{data.case_id}</dd>
        <dt className="font-semibold">Required roles</dt><dd>{data.required_roles.join(', ')}</dd>
        <dt className="font-semibold">Policy hash</dt><dd className="font-mono text-sm">{data.policy_hash}</dd>
        <dt className="font-semibold">Rules fired</dt><dd>{data.rules_fired.map(String).join(', ') || 'none'}</dd>
      </dl>
      <details open><summary className="font-semibold">Arguments, state before and after</summary>
        <Json value={{ args: data.args, state_before: data.state_before, state_after_if_approved: data.state_after_if_approved, compensation: data.compensation }} />
      </details>
      <aside aria-label="AI explanation" data-testid="agent-explanation" className="rounded border-2 border-dashed border-violet-500 bg-violet-50 p-3">
        <p className="text-sm font-bold text-violet-900">{data.agent_explanation.label}</p>
        <p>{data.agent_explanation.text}</p>
      </aside>
      <div className="flex gap-2">
        <button type="button" className={btnPrimary} onClick={() => decide('approve')}>Approve</button>
        <button type="button" className={btnDanger} onClick={() => decide('reject')}>Reject</button>
      </div>
      {done && <p role="status" className="text-emerald-800">Server accepted: {done}</p>}
      <ApprovalRefusal error={refusal} />
    </div>
  )
}

function HandoffRow({ h, approvals, onChange }: { h: Handoff; approvals: string[]; onChange: () => void }) {
  const { api, identity } = useLive()
  const [instr, setInstr] = useState('')
  const [err, setErr] = useState<unknown>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const guard = async (fn: () => Promise<string>) => {
    setErr(null)
    try {
      setMsg(await fn())
      onChange()
    } catch (e) {
      setErr(e)
    }
  }
  return (
    <li data-testid="handoff" className="rounded border border-slate-300 bg-white p-3">
      <div className="flex flex-wrap items-baseline gap-3">
        <b>{h.handoff_id}</b>
        <a className="text-sky-800 underline" href={`#/case/${h.case_id}`}>{h.case_id}</a>
        <span>{h.status} | owner {h.owner_role} | deadline <b>{h.deadline}</b>{h.verdict ? ` | verdict ${h.verdict}` : ''}</span>
        <button type="button" className={btn} aria-expanded={open} onClick={() => setOpen(!open)}>{open ? 'Hide packet' : 'Show packet'}</button>
      </div>
      {open && <Json value={h.packet} />}
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <label className="flex flex-col text-sm">Instruction to return to agent
          <input className={`${inputCls} w-80`} value={instr} onChange={(e) => setInstr(e.target.value)} />
        </label>
        <button type="button" className={btn} onClick={() => guard(async () => `Returned: ${(await api.returnHandoff(h.handoff_id, instr, identity)).status}`)}>Return to agent</button>
        <button type="button" className={btn} onClick={() => guard(async () => ((await api.verdict(h.handoff_id, 'needed')).ok ? 'Verdict: needed' : 'failed'))}>Handoff was needed</button>
        <button type="button" className={btn} onClick={() => guard(async () => ((await api.verdict(h.handoff_id, 'not_needed')).ok ? 'Verdict: not needed' : 'failed'))}>Not needed</button>
      </div>
      {msg && <p role="status" className="text-emerald-800">{msg}</p>}
      <ApprovalRefusal error={err} />
      {approvals.length > 0 && <p className="mt-1 text-sm">Pending approvals on this case: {approvals.join(', ')}</p>}
    </li>
  )
}

export function Handoffs() {
  const { api } = useLive()
  const rel = (e: { kind: string }) => e.kind === 'ESCALATION' || e.kind === 'HUMAN_ACTION' || e.kind === 'STATE'
  const hs = useResource(() => api.handoffs(), [api], rel)
  const ap = useResource(() => api.approvals('PENDING'), [api], rel)
  const [sel, setSel] = useState<string | null>(null)
  if (hs.error && !hs.data) return <ErrorNote error={hs.error} />
  if (!hs.data) return <Loading />
  const approvals = ap.data?.approvals ?? []
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-bold">Handoff queue</h1>
      <ol className="flex flex-col gap-2" aria-label="handoffs by deadline">
        {byDeadline(hs.data.handoffs).map((h) => (
          <HandoffRow key={h.handoff_id} h={h} approvals={approvals.filter((a) => a.case_id === h.case_id).map((a) => a.approval_id)} onChange={hs.reload} />
        ))}
        {hs.data.handoffs.length === 0 && <li>Queue is empty.</li>}
      </ol>
      <Panel title="Pending approvals">
        <ul className="mb-3 flex flex-wrap gap-2">
          {approvals.map((a) => (
            <li key={a.approval_id}>
              <button type="button" className={btn} onClick={() => setSel(a.approval_id)}>
                {a.approval_id}: {a.action} ({a.case_id}) needs {Array.isArray(a.role_required) ? a.role_required.join('/') : a.role_required}
              </button>
            </li>
          ))}
          {approvals.length === 0 && <li>None pending.</li>}
        </ul>
        {sel && <ApprovalDialog key={sel} approvalId={sel} onDecided={ap.reload} />}
      </Panel>
    </div>
  )
}
