import { useState } from 'react'
import type { ActionRow } from '../api/types'
import { useLive } from '../hooks/live'
import { undoState } from '../lib/undo'
import { ApprovalRefusal } from './ApprovalRefusal'
import { btn } from './common'

export function UndoButton({ action, onDone }: { action: ActionRow; onDone?: () => void }) {
  const { api, simTime, identity } = useLive()
  const st = undoState(action, simTime)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const [err, setErr] = useState<unknown>(null)
  const click = async () => {
    setBusy(true)
    setErr(null)
    try {
      const r = await api.undo(action.action_id, identity)
      setResult(r.error ? `${r.status}: ${r.error}` : r.status)
      if (r.violations?.length) setResult((x) => `${x} (${r.violations.map((v) => v.rule_id ?? v.reason).join(', ')})`)
      onDone?.()
    } catch (e) {
      setErr(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div>
      <button type="button" className={btn} disabled={!st.enabled || busy} onClick={click} aria-label={`Undo ${action.name}`}>
        {st.label}
      </button>
      {result && <p role="status" className="text-sm text-emerald-800">{result}</p>}
      <ApprovalRefusal error={err} />
    </div>
  )
}
