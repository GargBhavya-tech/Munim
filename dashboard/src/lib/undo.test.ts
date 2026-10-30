import { describe, expect, it } from 'vitest'
import { clockText, undoState } from './undo'

const base = { undoable: true, status: 'DONE', compensable_until: '2026-03-10T18:00:00+05:30' }

describe('undoState', () => {
  it('is enabled with a sim-time countdown while the window is open', () => {
    const s = undoState(base, '2026-03-10T14:30:00+05:30')
    expect(s.enabled).toBe(true)
    expect(s.remainingMs).toBe(3.5 * 3600_000)
    expect(s.label).toContain('3h 30m')
  })
  it('disables exactly at the deadline and after (advancing the sim clock closes it)', () => {
    expect(undoState(base, '2026-03-10T18:00:00+05:30').enabled).toBe(false)
    const after = undoState(base, '2026-03-11T18:00:00+05:30')
    expect(after.enabled).toBe(false)
    expect(after.label).toMatch(/closed/)
  })
  it('is disabled when the server says not undoable even inside the window', () => {
    expect(undoState({ ...base, undoable: false }, '2026-03-10T14:00:00+05:30').enabled).toBe(false)
  })
  it('is disabled without a compensable_until or without sim time', () => {
    expect(undoState({ ...base, compensable_until: null }, '2026-03-10T14:00:00+05:30').enabled).toBe(false)
    expect(undoState(base, null).enabled).toBe(false)
  })
  it('formats the receipt clock', () => {
    expect(clockText('2026-03-10T18:00:00+05:30')).toBe('6:00 PM')
    expect(clockText('2026-03-10T00:15:00+05:30')).toBe('12:15 AM')
  })
})
