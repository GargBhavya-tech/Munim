import { useEffect, useState } from 'react'
import { useLive } from '../hooks/live'
import { ErrorNote, Panel } from '../components/common'
import type { CoverageResponse, CoverageRow } from '../api/types'

const cell = 'px-2 py-1 align-top'

export function Coverage() {
  const { api } = useLive()
  const [data, setData] = useState<CoverageResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => {
    api.coverage().then(setData).catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [api])
  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-2xl font-bold">Coverage matrix</h1>
      <p className="text-sm text-slate-700">Computed from recorded test results only. Nothing here is marked by hand.</p>
      <ErrorNote error={err} />
      {data && (
        <>
          <div data-testid="coverage-summary" className="flex flex-wrap items-center gap-3 text-sm">
            <strong>{data.supported_count} of {data.playbooks.length} playbooks supported</strong>
            <span>Config {data.config_hash}</span>
            <span>{data.last_run ? `Last run #${data.last_run.run} at ${data.last_run.recorded_at}` : 'No results recorded'}</span>
            {data.stale && <span role="alert" data-testid="stale" className="rounded bg-amber-100 px-2 py-0.5 font-bold text-amber-900">Results are stale: run the tests again</span>}
          </div>
          <table className="w-full border-collapse text-left">
            <thead>
              <tr className="border-b-2 border-slate-400"><th className={cell}>Playbook</th><th className={cell}>Supported</th><th className={cell}>Dev passed</th>
                <th className={cell}>Held-out pass^{data.pass_k}</th><th className={cell}>Safety</th><th className={cell}>Why not</th></tr>
            </thead>
            <tbody>
              {data.playbooks.map((r) => <Row key={r.playbook_id} r={r} open={open === r.playbook_id} toggle={() => setOpen(open === r.playbook_id ? null : r.playbook_id)} />)}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}

function Row({ r, open, toggle }: { r: CoverageRow; open: boolean; toggle: () => void }) {
  return (
    <>
      <tr data-testid="coverage-row" data-supported={r.supported ? 'yes' : 'no'} className="border-b border-slate-200">
        <td className={`${cell} font-mono`}>{r.playbook_id}</td>
        <td className={cell}>{r.supported ? <span className="font-bold text-emerald-800">Supported</span> : <span className="font-bold text-red-800">Not supported</span>}</td>
        <td className={cell}>{r.dev.passed}/{r.dev.n}</td>
        <td className={cell}>{r.heldout.passed}/{r.heldout.n} ({r.heldout.attempts}/{r.heldout.pass_k} runs){r.heldout.pass_k_ok ? ' ✓' : ''}</td>
        <td className={cell}>{r.safety.passed}/{r.safety.n}</td>
        <td className={cell}>
          {r.why_not.length > 0 && <button type="button" aria-expanded={open} onClick={toggle} className="rounded border border-slate-400 px-2 py-0.5 text-sm font-medium hover:bg-slate-100">Kyun nahi?</button>}
        </td>
      </tr>
      {open && (
        <tr data-testid="why-not"><td colSpan={6} className="bg-slate-50 px-4 py-2">
          <Panel title={`Why ${r.playbook_id} is not supported`}><ul className="list-disc pl-5">{r.why_not.map((w) => <li key={w}>{w}</li>)}</ul></Panel>
        </td></tr>
      )}
    </>
  )
}
