import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ClaimChips } from './ClaimChips'
import { backTranslations } from '../lib/conversation'
import { LiveProvider } from '../hooks/live'
import { mockApi } from '../api/mock'
import { CaseView } from '../screens/CaseView'
import type { Claim } from '../api/types'

const blocked: Claim = { id: 'c4', text: 'The fee is 99 rupees.', source: 'tool:get_settlements#r1', level: 'UNSUPPORTED', color: 'red',
  reason: 'numbers_not_in_source: 99', rule_id: null, verify_seq: 12, nli_label: null, nli_score: null, snippet: null, corroborated_by: [] }
const sent: Claim = { id: 'c1', text: 'Settlement status is PENDING.', source: 'tool:get_settlements#r1', level: 'SUPPORTED', color: 'green',
  reason: null, rule_id: null, verify_seq: 12, nli_label: 'ENTAILMENT', nli_score: 0.97, snippet: 'Settlements status is PENDING.', corroborated_by: [] }

describe('#63 claim chips', () => {
  it('a turn with one blocked claim shows exactly one red chip with its reason and the would-be text', () => {
    render(<ClaimChips claims={[sent, blocked]} />)
    const red = screen.getAllByTestId('claim-chip').filter((c) => c.getAttribute('data-color') === 'red')
    expect(red).toHaveLength(1)
    expect(red[0].querySelector('[data-testid="chip-reason"]')!.textContent).toBe('numbers_not_in_source: 99')
    expect(red[0].querySelector('[data-testid="would-be"]')!.textContent).toContain('The fee is 99 rupees.')
  })
  it('a green chip carries its evidence: snippet and entailment score', () => {
    render(<ClaimChips claims={[sent]} />)
    expect(screen.getByTestId('chip-snippet').textContent).toBe('Settlements status is PENDING.')
    expect(screen.getByTestId('chip-evidence').textContent).toContain('ENTAILMENT (0.97)')
  })
  it('back-translations are matched by the exact text the gate checked', () => {
    const f = backTranslations([{ verify_seq: 1, status: 'SEND', language: 'hi-IN', sentences: [
      { kind: 'compliance', text: 'Main Munim hoon.', claim_ids: [], ok: true, back_translation: null },
      { kind: 'claim', text: 'Aapka settlement PENDING hai.', claim_ids: ['c1'], ok: true, back_translation: 'Your settlement is PENDING.' }] }])
    expect(f('Main Munim hoon. Aapka settlement PENDING hai.')).toBe('Main Munim hoon. Your settlement is PENDING.')
    expect(f('something else')).toBeNull()
  })
  it('the case view toggles the reply to the English back-translation', async () => {
    render(<LiveProvider mock api={mockApi}><CaseView caseId="CS-0002" /></LiveProvider>)
    await waitFor(() => expect(screen.getAllByTestId('message')).toHaveLength(2))
    expect(screen.getAllByTestId('message-text')[1].textContent).toBe('Maine naya device order kar diya hai.')
    fireEvent.click(screen.getByTestId('toggle-english'))
    expect(screen.getAllByTestId('message-text')[1].textContent).toBe('I have ordered a new device.')
  })
})
