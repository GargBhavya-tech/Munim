import { useState } from 'react'
import { useLive, useResource } from '@old/hooks/live'
import type { Handoff } from '@old/api/types'
import { Button, Stamp, Tag } from '../ui'
import { field, Json, loading, Note, Page, Refusal, Section } from '../parts'

/** Where Munim hands a case to a person. A person acts through the same policy as Munim: the server can refuse them, and its refusal shows here word for word. */

const byDeadline = (hs: Handoff[]): Handoff[] => hs.slice().sort((a, b) => Date.parse(a.deadline) - Date.parse(b.deadline))

function ApprovalDialog({ approvalId, onDecided }: { approvalId: string; onDecided?: () => void }) {
  const { api, identity } = useLive()
  const { data, error, reload } = useResource(() => api.approval(approvalId), [api, approvalId], (e) => e.kind === 'HUMAN_ACTION')
  const [refusal, setRefusal] = useState<unknown>(null)
  const [done, setDone] = useState<string | null>(null)
  if (error && !data) return <Note error={error} />
  if (!data) return loading
  const decide = async (v: 'approve' | 'reject') => {
    setRefusal(null)
    try { const r = await api.decide(approvalId, v, identity); setDone(r.status); reload(); onDecided?.() } catch (e) { setRefusal(e) }
  }
  return (
    <div className="border border-line bg-surface p-5" data-testid="approval-dialog">
      <h3 className="text-h3">Approval {data.approval_id}: {data.summary}</h3>
      <dl className="text-small mt-4 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
        <dt className="text-muted">Status</dt><dd>{data.status}</dd>
        <dt className="text-muted">Risk class</dt><dd>{data.risk_class}</dd>
        <dt className="text-muted">Case</dt><dd>{data.case_id}</dd>
        <dt className="text-muted">Required roles</dt><dd>{data.required_roles.join(', ')}</dd>
        <dt className="text-muted">Policy hash</dt><dd className="text-num break-all">{data.policy_hash}</dd>
        <dt className="text-muted">Rules fired</dt><dd>{data.rules_fired.map(String).join(', ') || 'none'}</dd>
      </dl>
      <details open className="mt-4"><summary className="text-label cursor-pointer text-muted">Arguments, state before and after</summary>
        <Json value={{ args: data.args, state_before: data.state_before, state_after_if_approved: data.state_after_if_approved, compensation: data.compensation }} />
      </details>
      <aside aria-label="AI explanation" className="mt-4 border border-dashed border-line p-3">
        <p className="text-label text-muted">{data.agent_explanation.label}</p>
        <p className="mt-1">{data.agent_explanation.text}</p>
      </aside>
      <div className="mt-4 flex gap-2">
        <Button variant="primary" onClick={() => void decide('approve')}>Approve</Button>
        <Button className="text-bad" onClick={() => void decide('reject')}>Reject</Button>
      </div>
      {done && <p role="status" className="mt-3"><Stamp tone="ok">server accepted · {done}</Stamp></p>}
      <Refusal error={refusal} />
    </div>
  )
}

function HandoffRow({ h, approvals, onChange }: { h: Handoff; approvals: string[]; onChange: () => void }) {
  const { api, identity } = useLive()
  const [instr, setInstr] = useState('')
  const [err, setErr] = useState<unknown>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const guard = async (fn: () => Promise<string>) => { setErr(null); try { setMsg(await fn()); onChange() } catch (e) { setErr(e) } }
  return (
    <li className="rule-b py-5">
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
        <b className="text-num">{h.handoff_id}</b>
        <a className="text-ink" href="#/floor">{h.case_id}</a>
        <Tag>{h.status}</Tag>
        <span className="text-small text-muted">owner {h.owner_role} · deadline <b className="text-fg">{h.deadline}</b>{h.verdict ? ` · verdict ${h.verdict}` : ''}</span>
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="text-label text-ink underline-offset-4 hover:underline">{open ? 'hide packet' : 'show packet'}</button>
      </div>
      {open && <Json value={h.packet} />}
      <div className="mt-3 flex flex-wrap items-end gap-2">
        <label className="text-small flex flex-col gap-1 text-muted">Instruction to return to the agent
          <input className={`${field} w-80 max-w-full`} value={instr} onChange={(e) => setInstr(e.target.value)} />
        </label>
        <Button onClick={() => void guard(async () => `Returned: ${(await api.returnHandoff(h.handoff_id, instr, identity)).status}`)}>Return to agent</Button>
        <Button onClick={() => void guard(async () => ((await api.verdict(h.handoff_id, 'needed')).ok ? 'Verdict: needed' : 'failed'))}>Handoff was needed</Button>
        <Button onClick={() => void guard(async () => ((await api.verdict(h.handoff_id, 'not_needed')).ok ? 'Verdict: not needed' : 'failed'))}>Not needed</Button>
      </div>
      {msg && <p role="status" className="text-small mt-2 text-ok">{msg}</p>}
      <Refusal error={err} />
      {approvals.length > 0 && <p className="text-small mt-2 text-muted">Pending approvals on this case: {approvals.join(', ')}</p>}
    </li>
  )
}

export function Handoffs() {
  const { api } = useLive()
  const rel = (e: { kind: string }) => e.kind === 'ESCALATION' || e.kind === 'HUMAN_ACTION' || e.kind === 'STATE'
  const hs = useResource(() => api.handoffs(), [api], rel)
  const ap = useResource(() => api.approvals('PENDING'), [api], rel)
  const [sel, setSel] = useState<string | null>(null)
  const approvals = ap.data?.approvals ?? []
  return (
    <Page eyebrow="Handoffs" title="Cases a person has to carry." intro="Each handoff arrives with a packet: what was said, what was done, what is left. Returning it to the agent, or approving a held action, is recorded in the ledger like anything else.">
      {hs.error && !hs.data ? <Note error={hs.error} /> : !hs.data ? loading : (
        <Section title="Handoff queue" note="Ordered by deadline.">
          <ol aria-label="handoffs by deadline">
            {byDeadline(hs.data.handoffs).map((h) => <HandoffRow key={h.handoff_id} h={h} approvals={approvals.filter((a) => a.case_id === h.case_id).map((a) => a.approval_id)} onChange={hs.reload} />)}
            {hs.data.handoffs.length === 0 && <li className="text-muted">The queue is empty.</li>}
          </ol>
        </Section>
      )}
      <Section title="Pending approvals" note="A held action waits here for the role that may release it.">
        <ul className="mb-4 flex flex-wrap gap-2">
          {approvals.map((a) => (
            <li key={a.approval_id}><Button aria-pressed={sel === a.approval_id} onClick={() => setSel(a.approval_id)}>{a.approval_id}: {a.action} ({a.case_id}) needs {Array.isArray(a.role_required) ? a.role_required.join('/') : a.role_required}</Button></li>
          ))}
          {approvals.length === 0 && <li className="text-muted">None pending.</li>}
        </ul>
        {sel && <ApprovalDialog key={sel} approvalId={sel} onDecided={ap.reload} />}
      </Section>
    </Page>
  )
}
