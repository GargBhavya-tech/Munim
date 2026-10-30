import type { JudgeCaps, JudgeUsage, Scoreboard } from './types'

const ROWS: { key: keyof Omit<Scoreboard, 'label'>; label: string }[] = [
  { key: 'attempts', label: 'Attempts' },
  { key: 'blocked_claims', label: 'Blocked claims' },
  { key: 'refused_actions', label: 'Refused actions' },
  { key: 'escalations', label: 'Escalations' },
  { key: 'forbidden_executed', label: 'Forbidden executed' },
]

/** Shows the server's ledger counts verbatim. Nothing is computed here. `onFine` is the "that was fine" button. */
export function ScoreboardPanel({ board, usage, caps, onFine }: { board: Scoreboard; usage?: JudgeUsage; caps?: JudgeCaps; onFine?: () => void }) {
  return (
    <section aria-label="Judge scoreboard" data-testid="scoreboard" className="rounded border-2 border-slate-400 p-3">
      <p className="text-xs font-bold uppercase tracking-wide text-slate-600">{board.label} · counted from the ledger</p>
      <dl className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
        {ROWS.map((r) => {
          const bad = r.key === 'forbidden_executed' && board[r.key] > 0
          return (
            <div key={r.key} data-testid={`score-${r.key}`} data-bad={bad} className={`rounded border p-2 ${bad ? 'border-red-600 bg-red-100 text-red-900' : 'border-slate-300'}`}>
              <dt className="text-sm">{r.label}</dt>
              <dd className="text-2xl font-bold">{board[r.key]}</dd>
            </div>
          )
        })}
      </dl>
      {usage && caps && (
        <p className="mt-2 text-sm text-slate-700" data-testid="caps-line">
          Messages {usage.messages}/{caps.max_messages} · tokens {usage.tokens}/{caps.max_tokens} · cost {usage.cost_paise}/{caps.max_cost_paise} paise (simulated rate)
        </p>
      )}
      {onFine && (
        <button type="button" onClick={onFine} className="mt-2 rounded border border-emerald-700 px-3 py-1 text-emerald-900">
          That was fine{board.fine_marks > 0 ? ` (${board.fine_marks})` : ''}
        </button>
      )}
    </section>
  )
}
