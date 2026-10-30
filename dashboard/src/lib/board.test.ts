import { describe, expect, it } from 'vitest'
import type { Case } from '../api/types'
import { groupByState, upsertCase } from './board'

const mk = (id: string, state: string): Case => ({ case_id: id, merchant_id: 'M', lead_id: null, playbook_id: 'P', state, lane: 'SUPPORT', language: 'hi', step: null, touches: 0, opened_at: '', closed_at: null, resume_note: null })

describe('board grouping', () => {
  it('groups by state in lifecycle order, unknown states after, from data only', () => {
    const cols = groupByState([mk('CS-3', 'RESOLVED'), mk('CS-1', 'NEW'), mk('CS-2', 'NEW'), mk('CS-4', 'WEIRD_NEW_STATE')])
    expect(cols.map((c) => c.state)).toEqual(['NEW', 'RESOLVED', 'WEIRD_NEW_STATE'])
    expect(cols[0].cases.map((c) => c.case_id)).toEqual(['CS-1', 'CS-2'])
  })
  it('a changed case moves columns', () => {
    const next = upsertCase([mk('CS-1', 'NEW')], mk('CS-1', 'ACTIVE'))
    expect(groupByState(next).map((c) => c.state)).toEqual(['ACTIVE'])
    expect(upsertCase(next, mk('CS-9', 'NEW'))).toHaveLength(2)
  })
})
