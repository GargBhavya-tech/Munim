import { describe, expect, it } from 'vitest'
import type { LedgerEntry } from '../api/types'
import { initialPager, pagerReducer, visibleEntries } from './ledgerPager'

const e = (seq: number, kind = 'EVENT'): LedgerEntry => ({ seq, case_id: null, ts_sim: '', actor: 'A', role: null, kind, payload: {}, policy_hash: null, model_id: null, hash: 'h' })

describe('ledger pager', () => {
  it('appends pages, advances cursor, dedupes, marks done on a short page', () => {
    let s = initialPager(2)
    s = pagerReducer(s, { type: 'page', page: { entries: [e(1), e(2)], next_after_seq: 2 }, requestedLimit: 2 })
    expect(s.done).toBe(false)
    expect(s.afterSeq).toBe(2)
    s = pagerReducer(s, { type: 'page', page: { entries: [e(2), e(3)], next_after_seq: 3 }, requestedLimit: 3 })
    expect(s.entries.map((x) => x.seq)).toEqual([1, 2, 3])
    expect(s.done).toBe(true)
  })
  it('filtering by kind resets the loaded pages and filters visible rows', () => {
    let s = initialPager(2)
    s = pagerReducer(s, { type: 'page', page: { entries: [e(1, 'VERIFY'), e(2, 'EVENT')], next_after_seq: 2 }, requestedLimit: 2 })
    s = pagerReducer(s, { type: 'toggleKind', kind: 'VERIFY' })
    expect(s.entries).toEqual([])
    expect(s.afterSeq).toBe(0)
    s = pagerReducer(s, { type: 'page', page: { entries: [e(1, 'VERIFY'), e(2, 'EVENT')], next_after_seq: 2 }, requestedLimit: 2 })
    expect(visibleEntries(s).map((x) => x.seq)).toEqual([1])
  })
})
