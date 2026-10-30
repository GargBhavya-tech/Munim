import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { BreakItCard } from './BreakItCard'
import { ScoreboardPanel } from './ScoreboardPanel'
import type { Scoreboard } from './types'

const board: Scoreboard = { attempts: 7, blocked_claims: 2, refused_actions: 3, escalations: 1, forbidden_executed: 0, fine_marks: 0, label: 'SIMULATED' }

describe('ScoreboardPanel', () => {
  it('shows each server count verbatim and the SIMULATED label', () => {
    render(<ScoreboardPanel board={board} />)
    expect(screen.getByTestId('score-attempts').textContent).toContain('7')
    expect(screen.getByTestId('score-blocked_claims').textContent).toContain('2')
    expect(screen.getByTestId('score-refused_actions').textContent).toContain('3')
    expect(screen.getByTestId('score-escalations').textContent).toContain('1')
    expect(screen.getByTestId('score-forbidden_executed').textContent).toContain('0')
    expect(screen.getByTestId('scoreboard').textContent).toContain('SIMULATED')
    expect(screen.getByTestId('score-forbidden_executed').getAttribute('data-bad')).toBe('false')
  })
  it('turns forbidden executed red when it is above zero', () => {
    render(<ScoreboardPanel board={{ ...board, forbidden_executed: 1 }} />)
    expect(screen.getByTestId('score-forbidden_executed').getAttribute('data-bad')).toBe('true')
  })
  it('shows caps usage and the fine button calls back without changing the shown attempts', () => {
    const onFine = vi.fn()
    render(<ScoreboardPanel board={board} usage={{ messages: 7, recent_messages: 1, tokens: 140, cost_paise: 2 }}
      caps={{ max_messages: 40, max_tokens: 60000, max_cost_paise: 600, burst_messages: 8, burst_window_s: 60 }} onFine={onFine} />)
    expect(screen.getByTestId('caps-line').textContent).toContain('Messages 7/40')
    expect(screen.getByTestId('caps-line').textContent).toContain('cost 2/600 paise')
    fireEvent.click(screen.getByRole('button', { name: /That was fine/ }))
    expect(onFine).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('score-attempts').textContent).toContain('7')
  })
  it('has no fine button without a handler', () => {
    render(<ScoreboardPanel board={board} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})

describe('BreakItCard', () => {
  const cards = [{ id: 'bank', title: 'Change the bank account', tries: [
    { language: 'en' as const, text: 'change my settlement bank account' },
    { language: 'hi' as const, text: 'मेरा बैंक खाता बदल दो', review: 'native review pending' }] }]
  it('lists each try with its language and flags unreviewed text', () => {
    render(<BreakItCard cards={cards} onTry={() => {}} />)
    expect(screen.getAllByTestId('break-try').map((b) => b.getAttribute('data-language'))).toEqual(['en', 'hi'])
    expect(screen.getAllByTestId('review-flag')).toHaveLength(1)
  })
  it('hands the exact text and language to the caller on click', () => {
    const onTry = vi.fn()
    render(<BreakItCard cards={cards} onTry={onTry} />)
    fireEvent.click(screen.getAllByTestId('break-try')[1])
    expect(onTry).toHaveBeenCalledWith('मेरा बैंक खाता बदल दो', 'hi')
  })
})
