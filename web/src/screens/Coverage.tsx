import { useEffect, useState } from 'react'
import { useLive } from '@old/hooks/live'
import type { CoverageResponse, CoverageRow } from '@old/api/types'
import { Stamp } from '../ui'
import { Note, Page } from '../parts'

/** What Munim is proven to handle. Computed from recorded test results only: nothing here is marked by hand. */

export function Coverage() {
  const { api } = useLive()
  const [data, setData] = useState<CoverageResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => { api.coverage().then(setData).catch((e) => setErr(e instanceof Error ? e.message : String(e))) }, [api])
  return (
    <Page eyebrow="Coverage" title="What Munim can be trusted with." intro="A playbook counts as supported only when its recorded results clear every floor. Anything that does not says why.">
      <Note error={err} />
      {data && (
        <>
          <div className="text-small flex flex-wrap items-center gap-x-6 gap-y-2 text-muted">
            <strong className="text-fg">{data.supported_count} of {data.playbooks.length} playbooks supported</strong>
            <span className="text-num">config {data.config_hash}</span>
            <span>{data.last_run ? `last run #${data.last_run.run} at ${data.last_run.recorded_at}` : 'No results recorded'}</span>
            {data.stale && <Stamp tone="warn">results are stale: run the tests again</Stamp>}
          </div>
          <div className="mt-6 overflow-x-auto">
            <table className="w-full min-w-[44rem] border-collapse text-left">
              <thead><tr className="rule-b text-label text-muted">
                <th className="py-2 pr-4 font-normal">Playbook</th><th className="pr-4 font-normal">Status</th><th className="pr-4 font-normal">Dev passed</th>
                <th className="pr-4 font-normal">Held-out pass^{data.pass_k}</th><th className="pr-4 font-normal">Safety</th><th className="font-normal" />
              </tr></thead>
              <tbody>{data.playbooks.map((r) => <Row key={r.playbook_id} r={r} open={open === r.playbook_id} toggle={() => setOpen(open === r.playbook_id ? null : r.playbook_id)} />)}</tbody>
            </table>
          </div>
        </>
      )}
    </Page>
  )
}

function Row({ r, open, toggle }: { r: CoverageRow; open: boolean; toggle: () => void }) {
  return (
    <>
      <tr className="rule-b" data-supported={r.supported ? 'yes' : 'no'}>
        <td className="text-num py-3 pr-4">{r.playbook_id}</td>
        <td className="pr-4">{r.supported ? <Stamp tone="ok">supported</Stamp> : <Stamp tone="bad">not supported</Stamp>}</td>
        <td className="text-num pr-4">{r.dev.passed}/{r.dev.n}</td>
        <td className="text-num pr-4">{r.heldout.passed}/{r.heldout.n} ({r.heldout.attempts}/{r.heldout.pass_k} runs){r.heldout.pass_k_ok ? ' ✓' : ''}</td>
        <td className="text-num pr-4">{r.safety.passed}/{r.safety.n}</td>
        <td>{r.why_not.length > 0 && <button type="button" aria-expanded={open} onClick={toggle} className="text-label min-h-9 text-stamp underline-offset-4 hover:underline">Kyun nahi?</button>}</td>
      </tr>
      {open && (
        <tr><td colSpan={6} className="bg-surface px-4 py-4">
          <p className="text-label text-muted">Why {r.playbook_id} is not supported</p>
          <ul className="mt-2 list-disc pl-5">{r.why_not.map((w) => <li key={w}>{w}</li>)}</ul>
        </td></tr>
      )}
    </>
  )
}
