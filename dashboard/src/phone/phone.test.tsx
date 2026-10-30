import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PhoneClient } from './PhoneClient'
import { encodeWav } from './wav'

const board = { attempts: 0, blocked_claims: 0, refused_actions: 0, escalations: 0, forbidden_executed: 0, fine_marks: 0, label: 'SIMULATED' }
const view = { session: { session_id: 'JS-5', persona_id: 'P-2', merchant_id: 'M-0002', start_seq: 5 }, scoreboard: board,
  usage: { messages: 0, recent_messages: 0, tokens: 0, cost_paise: 0 }, caps: { max_messages: 40, max_tokens: 60000, max_cost_paise: 600, burst_messages: 8, burst_window_s: 60 } }
const json = (status: number, body: unknown) => Promise.resolve({ ok: status < 300, status, json: () => Promise.resolve(body) } as Response)
afterEach(() => vi.unstubAllGlobals())

describe('encodeWav', () => {
  it('writes a RIFF/WAVE 16-bit mono header and clamped samples', () => {
    const w = encodeWav(new Float32Array([0, 1, -1, 2]), 16000)
    const dv = new DataView(w.buffer)
    expect(String.fromCharCode(...w.slice(0, 4))).toBe('RIFF')
    expect(String.fromCharCode(...w.slice(8, 12))).toBe('WAVE')
    expect(dv.getUint32(24, true)).toBe(16000)
    expect(dv.getUint32(40, true)).toBe(8)
    expect([1, 2, 3, 4].map((i) => dv.getInt16(44 + (i - 1) * 2, true))).toEqual([0, 32767, -32768, 32767])
  })
})

describe('PhoneClient', () => {
  it('redeems the token once (even under StrictMode-style double render), shows SIMULATED, persona and scoreboard', async () => {
    const f = vi.fn(() => json(200, view)); vi.stubGlobal('fetch', f)
    render(<PhoneClient token="jt_abc" />)
    await screen.findByTestId('phone-client')
    expect(f).toHaveBeenCalledTimes(1)
    expect(JSON.parse((f.mock.calls[0] as any)[1].body)).toEqual({ token: 'jt_abc' })
    expect(screen.getByTestId('phone-simulated').textContent).toContain('SIMULATED')
    expect(screen.getByTestId('phone-simulated').textContent).toContain('P-2')
    expect(screen.getByTestId('score-attempts').textContent).toContain('0')
  })
  it('shows a used-token message and no input', async () => {
    vi.stubGlobal('fetch', vi.fn(() => json(409, { detail: { error: 'TOKEN_USED' } })))
    render(<PhoneClient token="jt_abc" />)
    expect((await screen.findByTestId('phone-error')).textContent).toContain('already used')
    expect(screen.queryByLabelText('Message')).toBeNull()
  })
  it('sends text to the session route and shows the reply and the new scoreboard', async () => {
    const f = vi.fn((url: string) => url.includes('/messages')
      ? json(200, { replies: [{ case_id: 'C1', language: 'en', text: 'Settlements status is PENDING.' }], ...view, scoreboard: { ...board, attempts: 1 } })
      : json(200, view))
    vi.stubGlobal('fetch', f)
    render(<PhoneClient token="jt_abc" />)
    const input = await screen.findByLabelText('Message')
    fireEvent.change(input, { target: { value: 'settlement nahi aaya' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await screen.findByText('Settlements status is PENDING.')
    expect(f.mock.calls.some((c) => c[0] === '/api/judge/sessions/JS-5/messages')).toBe(true)
    await waitFor(() => expect(screen.getByTestId('score-attempts').textContent).toContain('1'))
  })
  it('hold-to-talk sends the setup frame with the session merchant, then WAV bytes', async () => {
    vi.stubGlobal('fetch', vi.fn(() => json(200, view)))
    const sent: unknown[] = []
    const sock = { send: (x: unknown) => sent.push(x) } as unknown as WebSocket
    const record = async () => ({ stop: async () => ({ samples: new Float32Array([0, 0.5]), rate: 16000 }) })
    render(<PhoneClient token="jt_abc" makeSocket={() => sock} record={record} />)
    const b = await screen.findByTestId('hold-to-talk')
    fireEvent.pointerDown(b)
    await waitFor(() => expect(b.getAttribute('data-talking')).toBe('true'))
    fireEvent.pointerUp(b)
    await waitFor(() => expect(sent).toHaveLength(2))
    expect(JSON.parse(sent[0] as string)).toEqual({ merchant_id: 'M-0002', language: 'unknown', hinglish: false })
    expect((sent[1] as Uint8Array).length).toBe(48)
  })
})
