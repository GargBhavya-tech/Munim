import { useCallback, useEffect, useReducer, useState } from 'react'
import { useLive } from '../hooks/live'
import { initialPager, pagerReducer, visibleEntries } from '../lib/ledgerPager'
import { btn, btnPrimary, ErrorNote } from '../components/common'
import type { ReplayResponse, VerifyResult } from '../api/types'
import { isRefusal, RefusalWhy } from '../components/RefusalWhy'

const KINDS = ['EVENT', 'CONTEXT_HASH', 'DECISION', 'VERIFY', 'POLICY', 'ACTION_INTENT', 'ACTION_RESULT', 'COMPENSATION', 'MESSAGE_SENT', 'ESCALATION', 'HUMAN_ACTION', 'STATE']
const PAGE = 50

export function Audit() {
  const { api } = useLive()
  const [s, dispatch] = useReducer(pagerReducer, PAGE, initialPager)
  const [caseId, setCaseId] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [ver, setVer] = useState<VerifyResult | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [replay, setReplay] = useState<ReplayResponse | null>(null)
  const doReplay = async () => {
    if (!caseId) return
    try {
      setReplay(await api.replay(caseId))
      setErr(null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  const more = useCallback(
    async (after: number) => {
      dispatch({ type: 'loading' })
      try {
        const page = await api.ledger(after, PAGE, caseId || undefined)
        dispatch({ type: 'page', page, requestedLimit: PAGE })
        setErr(null)
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e))
        dispatch({ type: 'page', page: { entries: [], next_after_seq: after }, requestedLimit: 0 })
      }
    },
    [api, caseId],
  )
  useEffect(() => {
    dispatch({ type: 'reset' })
    void more(0)
  }, [more])

  const verify = async () => {
    setVerifying(true)
    try {
      setVer(await api.verify())
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setVerifying(false)
    }
  }
  const rows = visibleEntries(s)
  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-2xl font-bold">Audit ledger</h1>
      <div className="flex flex-wrap items-end gap-3">
        <button type="button" className={btnPrimary} onClick={verify} disabled={verifying}>Verify chain</button>
        <label className="flex flex-col text-sm">Case filter
          <input className="rounded border border-slate-400 px-2 py-1" value={caseId} onChange={(e) => setCaseId(e.target.value.trim())} placeholder="CS-0001" />
        </label>
        <button type="button" className={btn} onClick={doReplay} disabled={!caseId}>Replay case</button>
        {caseId && <a className="underline" data-testid="evidence-link" href={`/api/cases/${encodeURIComponent(caseId)}/evidence`} target="_blank" rel="noreferrer">Evidence pack</a>}
        <div role="status" aria-live="polite" data-testid="verify-result">
          {ver && (ver.ok
            ? <span className="font-bold text-emerald-800">Chain OK: {ver.entries} entries verified</span>
            : <span className="font-bold text-red-800">Chain BROKEN at seq {ver.first_mismatch}: {ver.reason}</span>)}
        </div>
      </div>
      {replay && (
        <section data-testid="replay" className="rounded border border-slate-300 p-2">
          <h2 className="font-semibold">Replay of {replay.case_id} from the ledger alone: {replay.chain.ok ? 'chain OK' : `chain BROKEN at ${replay.chain.first_mismatch}`}, state hash <code>{replay.state_hash.slice(0, 16)}</code></h2>
          <ol className="text-sm">{replay.steps.map((st) => <li key={st.seq} data-testid="replay-step"><span className="font-mono">#{st.seq}</span> {st.summary}</li>)}</ol>
        </section>
      )}
      <fieldset className="flex flex-wrap gap-2">
        <legend className="text-sm font-semibold">Kinds (filters the pages loaded so far)</legend>
        {KINDS.map((k) => (
          <label key={k} className="flex items-center gap-1 text-sm">
            <input type="checkbox" checked={s.kinds.includes(k)} onChange={() => { dispatch({ type: 'toggleKind', kind: k }); void more(0) }} />
            {k}
          </label>
        ))}
      </fieldset>
      <ErrorNote error={err} />
      <table className="w-full border-collapse text-left">
        <thead><tr className="border-b-2 border-slate-400"><th>Seq</th><th>Case</th><th>Kind</th><th>Actor</th><th>Sim time</th><th>Hash</th><th></th></tr></thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.seq} data-testid="ledger-row" className={`border-b border-slate-200 ${ver && !ver.ok && ver.first_mismatch === e.seq ? 'bg-red-200' : ''}`}>
              <td className="font-mono">{e.seq}</td><td>{e.case_id ?? '-'}</td><td>{e.kind}</td><td>{e.actor}</td><td className="text-sm">{e.ts_sim}</td>
              <td className="font-mono text-xs">{e.hash.slice(0, 12)}</td>
              <td>{isRefusal(e) && <RefusalWhy seq={e.seq} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex items-center gap-3">
        <button type="button" className={btn} disabled={s.done || s.loading} onClick={() => more(s.afterSeq)}>
          {s.loading ? 'Loading...' : s.done ? 'End of ledger' : 'Load next page'}
        </button>
        <span className="text-sm text-slate-600">{s.entries.length} entries loaded</span>
      </div>
    </div>
  )
}
