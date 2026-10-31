import type { LedgerEntry } from '@old/api/types'
import type { Tone } from './ui'

/** One honest line per ledger entry, built only from what the entry holds. Nothing is invented: an entry kind we do not recognise shows its kind. */
export function summarise(e: LedgerEntry): string {
  const p = e.payload as Record<string, unknown>
  const s = (k: string) => (typeof p[k] === 'string' ? (p[k] as string) : undefined)
  switch (e.kind) {
    case 'MESSAGE_SENT': return s('text') ?? 'message sent'
    case 'ACTION_INTENT': return `intends ${s('action') ?? 'an action'}`
    case 'ACTION_RESULT': return `${s('action') ?? 'action'} → ${s('status') ?? 'done'}`
    case 'POLICY': return p.allow === false ? `refused ${s('action') ?? 'an action'}${Array.isArray(p.rule_ids) ? ` (${(p.rule_ids as string[]).join(', ')})` : ''}` : `allowed ${s('action') ?? 'an action'}`
    case 'ESCALATION': return `handed to a person: ${Array.isArray(p.reasons) ? (p.reasons as string[]).join('; ') : 'escalated'}`
    case 'STATE': return s('reason') ? `${s('reason')}${s('state') ? ` → ${s('state')}` : ''}` : (s('state') ?? 'state change')
    case 'EVENT': return s('type') ?? 'event'
    case 'HUMAN_ACTION': return s('op') ?? 'human action'
    case 'VERIFY': return 'claims checked against their sources'
    case 'DECISION': return 'the model proposed a decision'
    default: return e.kind.toLowerCase().replace(/_/g, ' ')
  }
}

/** The colour of an entry's rail: refusals and hand-overs are the accent, a clean action is ok, everything else is ink. */
export function toneOf(e: LedgerEntry): Tone {
  const p = e.payload as Record<string, unknown>
  if (e.kind === 'ESCALATION') return 'stamp'
  if (e.kind === 'POLICY' && p.allow === false) return 'stamp'
  if (e.kind === 'ACTION_RESULT' && p.status === 'OK' && p.cls !== 'R0') return 'ok'
  return 'ink'
}
