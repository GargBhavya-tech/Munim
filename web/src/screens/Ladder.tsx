import { useEffect, useState } from 'react'
import { useLive } from '@old/hooks/live'
import type { LadderResponse } from '@old/api/types'
import { Stamp, type Tone } from '../ui'
import { Note, Page } from '../parts'

const TONE: Record<string, Tone> = { L0: 'muted', L1: 'warn', L2: 'ok' }

export function Ladder() {
  const { api } = useLive()
  const [data, setData] = useState<LadderResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { api.ladder().then(setData).catch((e) => setErr(e instanceof Error ? e.message : String(e))) }, [api])
  return (
    <Page eyebrow="Rollout ladder" title="How far each playbook is trusted." intro="A playbook moves up one level at a time, only when every floor holds, and drops straight down when one breaks.">
      <Note error={err} />
      {data && (
        <>
          <p className="text-small text-muted">{(['L0', 'L1', 'L2'] as const).map((l) => `${l} ${data.levels[l]}`).join(' · ')}</p>
          <div className="mt-6 overflow-x-auto">
            <table className="w-full min-w-[40rem] border-collapse text-left">
              <thead><tr className="rule-b text-label text-muted"><th className="py-2 pr-4 font-normal">Playbook</th><th className="pr-4 font-normal">Level</th><th className="pr-4 font-normal">Why</th><th className="font-normal">Since</th></tr></thead>
              <tbody>
                {data.playbooks.map((p) => (
                  <tr key={p.playbook_id} className="rule-b" data-level={p.level}>
                    <td className="text-num py-3 pr-4">{p.playbook_id}</td>
                    <td className="pr-4"><Stamp tone={TONE[p.level] ?? 'ink'}>{p.level}</Stamp></td>
                    <td className="text-small pr-4">{p.reason ?? 'no change recorded'}</td>
                    <td className="text-small text-muted">{p.updated_at ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Page>
  )
}
