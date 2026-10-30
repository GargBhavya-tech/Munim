import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { TourPanel } from './TourPanel'
import type { TourProgress, TourStep } from './types'

const steps: TourStep[] = [
  { id: 'start', title: 'Open a persona', ui_element: 'scoreboard', instruction: 'Pick one.', proves: 'A start entry.', clip: 'tour/clips/01.webm', clip_available: false },
  { id: 'first_message', title: 'Say something ordinary', ui_element: 'score-attempts', instruction: 'Type.', proves: 'An attempt entry.', clip: 'tour/clips/02.webm', clip_available: false },
]
const progress: TourProgress = { done: 1, total: 2, next: 'first_message', label: 'SIMULATED', steps: [
  { id: 'start', done: true, metric: 'session_started', value: 1, op: '>=', needed: 1, evidence_seq: 7 },
  { id: 'first_message', done: false, metric: 'attempts', value: 0, op: '>=', needed: 1, evidence_seq: null } ] }

describe('TourPanel', () => {
  it('marks only server-proven steps done, shows the proving ledger seq and targets the named element', () => {
    render(<TourPanel steps={steps} progress={progress} />)
    const rows = screen.getAllByTestId('tour-step')
    expect(rows.map((r) => r.getAttribute('data-done'))).toEqual(['true', 'false'])
    expect(rows.map((r) => r.getAttribute('data-target'))).toEqual(['scoreboard', 'score-attempts'])
    expect(screen.getByTestId('tour-evidence').textContent).toContain('7')
    expect(screen.getByTestId('tour').textContent).toContain('1/2 proven by the ledger')
  })
  it('falls back to the caption when the clip is missing, only for the current step', () => {
    render(<TourPanel steps={steps} progress={progress} />)
    expect(screen.getAllByTestId('tour-clip-missing')).toHaveLength(1)
    expect(screen.getByTestId('tour-clip-missing').textContent).toContain('An attempt entry.')
  })
  it('plays the clip when it exists', () => {
    render(<TourPanel steps={steps.map((s) => ({ ...s, clip_available: true }))} progress={progress} />)
    expect(screen.getByTestId('tour-clip').getAttribute('src')).toBe('/tour/clips/02.webm')
  })
})
