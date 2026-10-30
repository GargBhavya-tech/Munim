import { useEffect, useState } from 'react'
import { useLive } from '../hooks/live'
import { ErrorNote } from '../components/common'
import type { LadderResponse } from '../api/types'

const COLOR: Record<string, string> = { L0: 'bg-slate-200 text-slate-900', L1: 'bg-amber-200 text-amber-950', L2: 'bg-emerald-200 text-emerald-950' }

export function Ladder() {
  const { api } = useLive()
  const [data, setData] = useState<LadderResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    api.ladder().then(setData).catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [api])
  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-2xl font-bold">Rollout ladder</h1>
      <p className="text-sm text-slate-700">How much each playbook is trusted to do on its own. A playbook moves up one level at a time, only when every floor holds, and drops straight down when one breaks.</p>
      <ErrorNote error={err} />
      {data && (
        <>
          <p data-testid="ladder-legend" className="text-sm">
            {(['L0', 'L1', 'L2'] as const).map((l) => `${l} ${data.levels[l]}`).join(' · ')}
          </p>
          <table className="w-full border-collapse text-left">
            <thead><tr className="border-b-2 border-slate-400"><th className="px-2 py-1">Playbook</th><th className="px-2 py-1">Level</th><th className="px-2 py-1">Why</th><th className="px-2 py-1">Since</th></tr></thead>
            <tbody>
              {data.playbooks.map((p) => (
                <tr key={p.playbook_id} data-testid="ladder-row" data-level={p.level} className="border-b border-slate-200">
                  <td className="px-2 py-1 font-mono">{p.playbook_id}</td>
                  <td className="px-2 py-1"><span className={`rounded px-2 py-0.5 font-bold ${COLOR[p.level]}`}>{p.level}</span></td>
                  <td className="px-2 py-1 text-sm">{p.reason ?? 'no change recorded'}</td>
                  <td className="px-2 py-1 text-sm">{p.updated_at ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
