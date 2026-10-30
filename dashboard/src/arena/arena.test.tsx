import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ArenaPage } from './ArenaPage'

afterEach(() => vi.restoreAllMocks())
const levels = [{ id: 'L1', order: 1, title: 'Move the money', goal: 'g1' }, { id: 'L2', order: 2, title: 'Skip the KYC', goal: 'g2' }]
const info = (state = 'RUNNING') => ({ label: 'SIMULATED', state, levels, leaderboard: [{ rank: 1, nickname: 'Asha', breaks: 0, blocked: 4, attempts: 5 }], budget: { spent_paise: 1, budget_paise: 9 } })

describe('ArenaPage', () => {
  it('shows levels, SIMULATED and the leaderboard from the server', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ status: 200, json: async () => info() } as Response)
    render(<ArenaPage />)
    await waitFor(() => expect(screen.getByTestId('level-L1')).toBeTruthy())
    expect(screen.getByText(/SIMULATED/)).toBeTruthy()
    expect(screen.getByTestId('leaderboard').textContent).toContain('Asha')
  })
  it('disables play and says so when the arena is stopped', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ status: 200, json: async () => info('BUDGET_STOPPED') } as Response)
    render(<ArenaPage />)
    await waitFor(() => expect(screen.getByTestId('arena-stopped').textContent).toContain('budget used up'))
    expect((screen.getByText('Play') as HTMLButtonElement).disabled).toBe(true)
  })
  it('shows the server reason when the nickname is rejected', async () => {
    const f = vi.spyOn(globalThis, 'fetch')
    f.mockResolvedValueOnce({ status: 200, json: async () => info() } as Response)
    f.mockResolvedValueOnce({ status: 400, json: async () => ({ detail: { error: 'NICKNAME_REJECTED', reason: 'that nickname is not allowed' } }) } as Response)
    f.mockResolvedValue({ status: 200, json: async () => info() } as Response)
    render(<ArenaPage />)
    await waitFor(() => screen.getByTestId('level-L1'))
    fireEvent.change(screen.getByLabelText('Nickname'), { target: { value: 'badname' } })
    fireEvent.click(screen.getByText('Play'))
    await waitFor(() => expect(screen.getByTestId('arena-msg').textContent).toBe('that nickname is not allowed'))
  })
})
