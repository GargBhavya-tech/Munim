import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SideBySide } from './SideBySide'
import { TwinBanner } from './TwinBanner'

afterEach(() => vi.restoreAllMocks())

describe('twin', () => {
  it('banner is a fixed constant', () => {
    render(<TwinBanner />)
    expect(screen.getByTestId('twin-banner').textContent).toBe('GUARDS OFF - SIMULATED TWIN')
  })
  it('banner is visible before, during and after an error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ status: 409, json: async () => ({ detail: { error: 'TWIN_NOT_STARTED' } }) } as Response)
    render(<SideBySide merchantId="M-0001" />)
    expect(screen.getByTestId('twin-banner')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByText('Send to both'))
    await waitFor(() => expect(screen.getByTestId('twin-error').textContent).toBe('TWIN_NOT_STARTED'))
    expect(screen.getByTestId('twin-banner')).toBeTruthy()
  })
  it('shows both replies side by side and the twin-only actions', async () => {
    const data = { banner: 'x', label: 'SIMULATED', real: { replies: [{ case_id: 'C', language: 'en', text: 'Real says pending.' }] },
      twin: { banner: 'x', label: 'SIMULATED', replies: [{ case_id: 'C', language: 'en', text: 'Twin says 5000.' }],
        actions: [{ action: 'send_message', status: 'OK', cls: 'W1' }, { action: 'get_x', status: 'OK', cls: 'R0' }], nonexistent_actions_requested: ['change_bank_details'] } }
    const f = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ status: 200, json: async () => data } as Response)
    render(<SideBySide merchantId="M-0001" />)
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'hello' } })
    fireEvent.click(screen.getByText('Send to both'))
    await waitFor(() => expect(screen.getByText('Twin says 5000.')).toBeTruthy())
    expect(screen.getByText('Real says pending.')).toBeTruthy()
    expect(screen.getByText('ran: send_message (OK)')).toBeTruthy()
    expect(screen.queryByText(/get_x/)).toBeNull()
    expect(screen.getByText(/asked for change_bank_details/)).toBeTruthy()
    expect(JSON.parse((f.mock.calls[0][1] as RequestInit).body as string)).toEqual({ merchant_id: 'M-0001', text: 'hello' })
  })
})
