import type { LedgerEntry } from '../api/types'

export type TileId = 'kyc' | 'bank' | 'device' | 'payments' | 'crm' | 'pricing' | 'scheduler' | 'tickets' | 'handoff' | 'comms'

// Heuristic: the contract does not map action names to systems, so we match on the name. Unknown names map to nothing.
const RULES: [RegExp, TileId][] = [
  [/kyc|pan_|aadhaar|name_match/i, 'kyc'],
  [/bank|penny|ifsc/i, 'bank'],
  [/device|soundbox|replace/i, 'device'],
  [/payment|settle|payout|refund/i, 'payments'],
  [/waiv|price|pricing|fee|plan/i, 'pricing'],
  [/follow_?up|book|schedul|callback/i, 'scheduler'],
  [/ticket/i, 'tickets'],
  [/handoff|escalat/i, 'handoff'],
  [/send|message|sms|whatsapp|comms|notify/i, 'comms'],
  [/case|crm|language|focus/i, 'crm'],
]

export const tileForAction = (name: string): TileId | null => RULES.find(([re]) => re.test(name))?.[1] ?? null

export interface TileCause {
  action: string
  key: string | null
  seq: number
}

/** Latest ACTION_RESULT/COMPENSATION per tile, from the ledger entries the stream has delivered. */
export function causesByTile(entries: LedgerEntry[]): Partial<Record<TileId, TileCause>> {
  const out: Partial<Record<TileId, TileCause>> = {}
  for (const e of entries) {
    if (e.kind !== 'ACTION_RESULT' && e.kind !== 'COMPENSATION') continue
    const action = typeof e.payload.action === 'string' ? e.payload.action : typeof e.payload.name === 'string' ? e.payload.name : null
    if (!action) continue
    const t = tileForAction(action)
    if (!t) continue
    const key = typeof e.payload.key === 'string' ? e.payload.key : null
    if (!out[t] || out[t]!.seq < e.seq) out[t] = { action, key, seq: e.seq }
  }
  return out
}
