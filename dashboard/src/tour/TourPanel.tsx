import type { TourProgress, TourStep } from './types'

/** Guided tour of Judge Mode. Steps are server data; done/not done and the proving ledger seq come from the ledger via the server. A missing clip falls back to the caption. */
export function TourPanel({ steps, progress }: { steps: TourStep[]; progress: TourProgress }) {
  const by = new Map(progress.steps.map((p) => [p.id, p]))
  return (
    <section aria-label="Guided tour" data-testid="tour" className="rounded border-2 border-slate-400 p-3">
      <p className="text-xs font-bold uppercase tracking-wide text-slate-600">SIMULATED · {progress.done}/{progress.total} proven by the ledger</p>
      <ol className="mt-2 flex flex-col gap-2">
        {steps.map((s, i) => {
          const p = by.get(s.id)
          const current = progress.next === s.id
          return (
            <li key={s.id} data-testid="tour-step" data-step={s.id} data-done={!!p?.done} data-current={current} data-target={s.ui_element}>
              <p className="font-semibold">{i + 1}. {s.title}{p?.done ? ' (done)' : ''}</p>
              <p className="text-sm">{s.instruction}</p>
              {p?.done && p.evidence_seq != null && <p className="text-xs text-slate-600" data-testid="tour-evidence">Ledger entry {p.evidence_seq}</p>}
              {current && (s.clip_available
                ? <video data-testid="tour-clip" src={`/${s.clip}`} controls />
                : <p data-testid="tour-clip-missing" className="text-xs text-slate-600">No recorded clip for this step. {s.proves}</p>)}
            </li>
          )
        })}
      </ol>
    </section>
  )
}
