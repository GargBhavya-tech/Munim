import { useLive, useResource } from '../hooks/live'
import { groupByState } from '../lib/board'
import { ErrorNote, Loading } from '../components/common'
import type { Case } from '../api/types'

export function CaseCard({ c }: { c: Case }) {
  return (
    <li className="rounded border border-slate-300 bg-white p-2 shadow-sm" data-testid="case-card">
      <a href={`#/case/${c.case_id}`} className="font-semibold text-sky-800 underline focus:outline-2 focus:outline-sky-600">
        {c.case_id}
      </a>
      <div className="text-sm">{c.playbook_id}</div>
      <div className="flex flex-wrap gap-1 text-sm">
        <span className="rounded bg-slate-200 px-1">{c.lane}</span>
        <span className="rounded bg-slate-200 px-1">{c.language}</span>
        <span className="rounded bg-slate-200 px-1">{c.touches} touches</span>
      </div>
    </li>
  )
}

export function CasesBoard() {
  const { api } = useLive()
  const { data, error } = useResource(() => api.cases(), [api], (e) => e.case_id !== null)
  if (error && !data) return <ErrorNote error={error} />
  if (!data) return <Loading />
  const cols = groupByState(data.cases)
  return (
    <div>
      <h1 className="mb-3 text-2xl font-bold">Cases ({data.cases.length})</h1>
      <ErrorNote error={error} />
      <div className="flex gap-3 overflow-x-auto pb-3">
        {cols.map((col) => (
          <section key={col.state} aria-label={`${col.state} cases`} className="min-w-56 flex-1 rounded-lg bg-slate-100 p-2" data-testid="board-column">
            <h2 className="mb-2 font-bold">
              {col.state} <span className="font-normal text-slate-600">({col.cases.length})</span>
            </h2>
            <ul className="flex flex-col gap-2">
              {col.cases.map((c) => (
                <CaseCard key={c.case_id} c={c} />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  )
}
