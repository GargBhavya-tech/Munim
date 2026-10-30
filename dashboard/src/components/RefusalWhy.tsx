import { useState } from 'react'
import { useLive } from '../hooks/live'
import type { LedgerEntry, WhyResponse } from '../api/types'

/** True for a POLICY ledger entry that refused an action. */
export const isRefusal = (e: LedgerEntry): boolean => e.kind === 'POLICY' && e.payload.allow === false

/** "Kyun nahi?": the server explains a refusal from rule metadata; nothing is generated in the browser. */
export function RefusalWhy({ seq }: { seq: number }) {
  const { api } = useLive()
  const [why, setWhy] = useState<WhyResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  return (
    <span>
      <button type="button" className="rounded border border-slate-400 px-2 py-0.5 text-sm font-medium hover:bg-slate-100"
        onClick={() => api.why(seq).then(setWhy).catch((e) => setErr(e instanceof Error ? e.message : String(e)))}>Kyun nahi?</button>
      {err && <span role="alert" className="ml-2 text-red-800">{err}</span>}
      {why && (
        <div data-testid="why" className="mt-1 rounded border border-slate-300 bg-slate-50 p-2 text-sm">
          <div>Action <span className="font-mono">{why.action}</span> was refused ({why.outcome}).</div>
          <ul className="list-disc pl-5">
            {why.rules.map((r) => (
              <li key={r.rule_id}><span className="font-mono font-bold">{r.rule_id}</span> {r.title} [{r.severity}]{r.reason ? `: ${r.reason}` : ''}
                {r.source && <span className="text-slate-600"> (source: {r.source})</span>}</li>
            ))}
          </ul>
        </div>
      )}
    </span>
  )
}
