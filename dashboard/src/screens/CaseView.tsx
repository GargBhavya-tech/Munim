import { useState } from 'react'
import { useLive, useResource } from '../hooks/live'
import { ClaimChips } from '../components/ClaimChips'
import { UndoButton } from '../components/UndoButton'
import { ErrorNote, Json, Loading, Panel, SimulatedBadge } from '../components/common'
import { clockText } from '../lib/undo'
import { backTranslations, speak } from '../lib/conversation'

export function CaseView({ caseId }: { caseId: string }) {
  const { api } = useLive()
  const [english, setEnglish] = useState(false)
  const { data, error, reload } = useResource(() => api.caseDetail(caseId), [api, caseId], (e) => e.case_id === caseId)
  if (error && !data) return <ErrorNote error={error} />
  if (!data) return <Loading />
  const { case: c, timeline, actions, claims, messages } = data
  const receipts = actions.filter((a) => a.undoable && a.compensable_until)
  const back = backTranslations(data.renders ?? [])
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">{c.case_id}</h1>
        <SimulatedBadge />
        <span>{c.playbook_id} | {c.lane} | {c.state} | {c.language} | {c.touches} touches</span>
      </div>
      <ErrorNote error={error} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Conversation">
          <label className="mb-2 flex items-center gap-2 text-sm">
            <input type="checkbox" checked={english} onChange={(e) => setEnglish(e.target.checked)} data-testid="toggle-english" />
            Show Munim's replies as the gate's English back-translation
          </label>
          <ul className="flex flex-col gap-2" aria-label="messages">
            {messages.map((m, i) => {
              const en = m.direction === 'OUT' ? back(m.text) : null
              return (
                <li key={i} data-testid="message" className={`max-w-[85%] rounded p-2 ${m.direction === 'IN' ? 'self-start bg-slate-200' : 'self-end bg-sky-100'}`}>
                  <span className="mr-2 rounded bg-slate-700 px-1 text-xs text-white" data-testid="lang-tag">{english && en ? 'en (back-translation)' : m.language}</span>
                  <span data-testid="message-text">{english && en ? en : m.text}</span>
                  <div className="flex items-center gap-2 text-xs text-slate-600">
                    {m.direction} | {m.ts_sim}
                    <button type="button" className="underline" onClick={() => speak(m.text, m.language)} aria-label={`Play message ${i + 1}`}>play</button>
                  </div>
                </li>
              )
            })}
            {messages.length === 0 && <li className="text-slate-600">No messages.</li>}
          </ul>
          {data.receipts
            ? data.receipts.map((r) => (
                <p key={r.action_id} data-testid="receipt" className="mt-3 rounded border border-emerald-600 bg-emerald-50 p-2">
                  {r.label} · {r.cancel} <span className="text-xs text-slate-600">(merchant receipt, say "wapas karo" to undo)</span>
                </p>
              ))
            : receipts.map((a) => (
                <p key={a.action_id} data-testid="receipt" className="mt-3 rounded border border-emerald-600 bg-emerald-50 p-2">
                  {a.name.replace(/_/g, ' ')} done · cancel until {clockText(a.compensable_until!)}
                </p>
              ))}
        </Panel>
        <Panel title="Claims (from the grounding gate)">
          <ClaimChips claims={claims} />
        </Panel>
        <Panel title="Actions">
          <ul className="flex flex-col gap-3">
            {actions.map((a) => (
              <li key={a.action_id} data-testid="action-row" className="rounded border border-slate-200 p-2">
                <div className="font-semibold">{a.name} <span className="font-normal text-slate-600">({a.cls}, {a.status})</span></div>
                <div className="text-sm text-slate-600">key: {a.idempotency_key}</div>
                <UndoButton action={a} onDone={reload} />
              </li>
            ))}
            {actions.length === 0 && <li className="text-slate-600">No actions.</li>}
          </ul>
        </Panel>
        <Panel title="Decision timeline (ledger)">
          <ol className="flex flex-col gap-1">
            {timeline.map((e) => (
              <li key={e.seq} data-testid="timeline-entry">
                <details>
                  <summary className="cursor-pointer">
                    <span className="font-mono text-sm">#{e.seq}</span> <b>{e.kind}</b> <span className="text-slate-600">{e.actor}{e.model_id ? ` (${e.model_id})` : ''}</span>
                  </summary>
                  <Json value={e.payload} />
                  <div className="font-mono text-xs break-all text-slate-600">policy {e.policy_hash ?? '-'} | hash {e.hash}</div>
                </details>
              </li>
            ))}
          </ol>
        </Panel>
      </div>
    </div>
  )
}
