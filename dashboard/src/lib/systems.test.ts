import { describe, expect, it } from 'vitest'
import type { LedgerEntry } from '../api/types'
import { causesByTile, tileForAction } from './systems'

const le = (seq: number, kind: string, payload: Record<string, unknown>): LedgerEntry => ({ seq, case_id: 'C', ts_sim: '', actor: 'A', role: null, kind, payload, policy_hash: null, model_id: null, hash: '' })

describe('causesByTile', () => {
  it('maps a replacement action result to the device tile with its key, latest wins', () => {
    const c = causesByTile([
      le(1, 'ACTION_RESULT', { action: 'order_device_replacement', key: 'k1', status: 'OK' }),
      le(2, 'ACTION_INTENT', { action: 'order_device_replacement', key: 'ignored' }),
      le(3, 'ACTION_RESULT', { action: 'order_device_replacement', key: 'k3', status: 'OK' }),
    ])
    expect(c.device).toEqual({ action: 'order_device_replacement', key: 'k3', seq: 3 })
  })
  it('unknown action names map to no tile', () => {
    expect(tileForAction('zzz_unknown')).toBeNull()
  })
})
