import { useState } from 'react'
import { policyApi, type CompileResult } from './api'

/** Type a rule in plain words. The server compiles it, shows the diff and the dry run, and only an ops_admin can approve. It can only tighten. */
export function PolicyCompiler({ role = 'ops_admin', principalId = 'u-ops', api = policyApi }: { role?: string; principalId?: string; api?: typeof policyApi }) {
  const [text, setText] = useState('')
  const [res, setRes] = useState<CompileResult | null>(null)
  const [msg, setMsg] = useState('')
  const draft = res && res.ok ? res.draft : null

  return (
    <section aria-label="Policy compiler" data-testid="policy-compiler" className="rounded border-2 border-slate-400 p-3">
      <p className="text-xs font-bold uppercase tracking-wide text-slate-600">SIMULATED · rules can only be tightened</p>
      <label className="mt-2 block text-sm">Rule in plain words
        <input data-testid="policy-text" className="mt-1 w-full rounded border p-1" value={text} onChange={(e) => setText(e.target.value)} placeholder="Block create_device_replacement for support executive" />
      </label>
      <button type="button" className="mt-2 rounded border px-3 py-1" onClick={async () => { setMsg(''); setRes(await api.compile(text)) }}>Compile</button>
      {res && !res.ok && <p role="alert" data-testid="policy-error" className="mt-2 text-red-800">{res.error.error}: {res.error.message}</p>}
      {draft && (
        <div data-testid="policy-draft" className="mt-2">
          <p className="font-semibold">{draft.summary} <span className="text-xs">({draft.source})</span></p>
          <pre data-testid="policy-diff" className="rounded bg-slate-100 p-2 text-sm">{draft.diff.lines.join('\n')}</pre>
          <p data-testid="policy-dryrun" data-ok={draft.ok} className="text-sm">
            Dry run: {draft.dry_run.probes_run} probes, {draft.dry_run.changed.length} changed, {draft.dry_run.loosened.length} weakened
          </p>
          <p className="text-xs text-slate-600">Rule {draft.rule_id} · hash {draft.rule_hash.slice(0, 12)}</p>
          <button type="button" disabled={!draft.ok} className="mt-2 rounded border border-emerald-700 px-3 py-1 disabled:opacity-40"
            onClick={async () => { const r = await api.approve(draft, principalId, role); setMsg(r.ok ? `Approved ${draft.rule_id}` : `${r.error?.error}: ${r.error?.message}`) }}>Approve</button>
          <button type="button" className="ml-2 rounded border px-3 py-1" onClick={async () => { const r = await api.revert(draft.rule_id, principalId, role); setMsg(r.ok ? `Reverted ${draft.rule_id}` : `${r.error?.error}: ${r.error?.message}`) }}>Revert</button>
        </div>
      )}
      {msg && <p data-testid="policy-msg" className="mt-2 text-sm">{msg}</p>}
    </section>
  )
}
