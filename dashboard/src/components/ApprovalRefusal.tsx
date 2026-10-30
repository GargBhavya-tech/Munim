import { ApiError } from '../api/client'

/** Shows the SERVER's refusal verbatim (rule id and reason). Nothing here decides anything. */
export function ApprovalRefusal({ error }: { error: unknown }) {
  if (!error) return null
  if (error instanceof ApiError) {
    const b = error.refusal.body
    return (
      <div role="alert" data-testid="refusal" className="rounded border-2 border-red-600 bg-red-50 p-3 text-red-900">
        <p className="font-bold">
          Server refused ({error.refusal.httpStatus}){b.rule_id ? `: ${b.rule_id}` : ''}
        </p>
        <p>{String(b.reason ?? (typeof b.detail === 'string' ? b.detail : '') ?? '')}</p>
      </div>
    )
  }
  return (
    <p role="alert" className="rounded border border-red-300 bg-red-50 p-2 text-red-800">
      {error instanceof Error ? error.message : String(error)}
    </p>
  )
}
