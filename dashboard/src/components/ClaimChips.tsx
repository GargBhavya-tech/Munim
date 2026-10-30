import type { Claim } from '../api/types'

const COLORS = { green: 'border-emerald-600 bg-emerald-100 text-emerald-900', amber: 'border-amber-600 bg-amber-100 text-amber-900', red: 'border-red-600 bg-red-100 text-red-900' }
const WHAT = { green: 'Sent', amber: 'Removed before sending', red: 'Blocked' }

/** Renders the gate's records (contract `claims`), never the reply text. Tap a chip for its evidence (#63). */
export function ClaimChips({ claims }: { claims: Claim[] }) {
  if (claims.length === 0) return <p className="text-slate-600" data-testid="claims-empty">No claim checks recorded yet (the grounding gate has not written any).</p>
  return (
    <ul className="flex flex-col gap-2">
      {claims.map((c) => (
        <li key={`${c.verify_seq ?? ''}-${c.id}`} data-testid="claim-chip" data-color={c.color} className={`rounded border-2 p-2 ${COLORS[c.color]}`}>
          <details>
            <summary className="cursor-pointer">
              <span className="font-semibold">{WHAT[c.color]} · {c.level}</span>: {c.color === 'green' ? c.text : <span data-testid="would-be">would have said “{c.text}”</span>}
            </summary>
            <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 text-sm" data-testid="chip-evidence">
              <dt>source</dt><dd className="break-all">{c.source}</dd>
              {c.reason && <><dt>reason</dt><dd data-testid="chip-reason">{c.reason}</dd></>}
              {c.rule_id && <><dt>rule</dt><dd data-testid="chip-rule">{c.rule_id}</dd></>}
              {c.nli_label && <><dt>entailment</dt><dd>{c.nli_label}{c.nli_score != null ? ` (${c.nli_score.toFixed(2)})` : ''}</dd></>}
              {c.corroborated_by && c.corroborated_by.length > 0 && <><dt>also backed by</dt><dd className="break-all">{c.corroborated_by.join(', ')}</dd></>}
              {c.snippet && <><dt>evidence</dt><dd data-testid="chip-snippet">{c.snippet}</dd></>}
              {c.verify_seq != null && <><dt>ledger</dt><dd>VERIFY #{c.verify_seq}</dd></>}
            </dl>
          </details>
        </li>
      ))}
    </ul>
  )
}
