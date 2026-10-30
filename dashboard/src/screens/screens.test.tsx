import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { LiveProvider, useLive } from '../hooks/live'
import { mockApi } from '../api/mock'
import { Audit } from './Audit'
import { CaseView } from './CaseView'
import { CasesBoard } from './CasesBoard'
import { Control } from './Control'
import { Coverage } from './Coverage'
import { Ladder } from './Ladder'
import { Judge } from './Judge'
import { Arena } from './Arena'
import { Phone } from './Phone'
import { ICON_UTTERANCES, Simple } from './Simple'
import { isRefusal, RefusalWhy } from '../components/RefusalWhy'
import type { LedgerEntry } from '../api/types'
import { Handoffs } from './Handoffs'
import { SystemsPanel, Tile } from './SystemsPanel'
import { ApprovalDialog } from './Handoffs'
import type { ReactNode } from 'react'

const wrap = (ui: ReactNode) => render(<LiveProvider mock api={mockApi}>{ui}</LiveProvider>)

describe('smoke: every screen renders from fixtures', () => {
  it('cases board shows 40 cards in data-driven columns', async () => {
    wrap(<CasesBoard />)
    await waitFor(() => expect(screen.getAllByTestId('case-card')).toHaveLength(40))
    expect(screen.getAllByTestId('board-column').length).toBeGreaterThan(3)
  })
  it('case view shows messages with language tags, claims, actions and undo state', async () => {
    wrap(<CaseView caseId="CS-0002" />)
    await waitFor(() => expect(screen.getAllByTestId('message')).toHaveLength(2))
    expect(screen.getAllByTestId('lang-tag')[0].textContent).toBe('hinglish')
    expect(screen.getAllByTestId('claim-chip').map((c) => c.getAttribute('data-color'))).toEqual(['green', 'amber', 'red'])
    const undo = screen.getAllByRole('button', { name: /^Undo / })
    expect((undo[0] as HTMLButtonElement).disabled).toBe(false) // inside window at sim 14:00, until 18:00
    expect(undo[0].textContent).toContain('4h 0m')
    expect((undo[1] as HTMLButtonElement).disabled).toBe(true) // window already closed
    expect((undo[2] as HTMLButtonElement).disabled).toBe(true) // no compensation
    expect(screen.getByTestId('receipt').textContent).toContain('cancel until 6:00 PM')
  })
  it('systems panel renders all ten tiles', async () => {
    wrap(<SystemsPanel />)
    await waitFor(() => expect(screen.getByTestId('tile-comms')).toBeTruthy())
    for (const id of ['kyc', 'bank', 'device', 'payments', 'crm', 'pricing', 'scheduler', 'tickets', 'handoff', 'comms'])
      expect(screen.getByTestId(`tile-${id}`)).toBeTruthy()
  })
  it('handoff queue is deadline ordered and shows the labelled AI explanation separately', async () => {
    wrap(<Handoffs />)
    await waitFor(() => expect(screen.getAllByTestId('handoff')).toHaveLength(2))
    expect(screen.getAllByTestId('handoff')[0].textContent).toContain('H-1') // earlier deadline first
    fireEvent.click(await screen.findByRole('button', { name: /AP-1/ }))
    const box = await screen.findByTestId('agent-explanation')
    expect(box.textContent).toContain('Written by the AI model')
    expect(screen.getByTestId('approval-dialog').textContent).toContain('override_kyc_name')
  })
  it('audit view loads one page, not the whole ledger, and verifies', async () => {
    wrap(<Audit />)
    await waitFor(() => expect(screen.getAllByTestId('ledger-row')).toHaveLength(50))
    fireEvent.click(screen.getByRole('button', { name: 'Verify chain' }))
    await waitFor(() => expect(screen.getByTestId('verify-result').textContent).toContain('Chain OK'))
    fireEvent.click(screen.getByRole('button', { name: 'Load next page' }))
    await waitFor(() => expect(screen.getAllByTestId('ledger-row')).toHaveLength(100))
  })
  it('audit view replays a case from the ledger and links its evidence pack', async () => {
    wrap(<Audit />)
    await waitFor(() => expect(screen.getAllByTestId('ledger-row').length).toBeGreaterThan(0))
    fireEvent.change(screen.getByPlaceholderText('CS-0001'), { target: { value: 'CS-0002' } })
    expect(screen.getByTestId('evidence-link').getAttribute('href')).toBe('/api/cases/CS-0002/evidence')
    fireEvent.click(screen.getByRole('button', { name: 'Replay case' }))
    await waitFor(() => expect(screen.getAllByTestId('replay-step')).toHaveLength(2))
    expect(screen.getByTestId('replay').textContent).toContain('chain OK')
  })
  it('control panel shows kill switch, clock, chaos and cost with assumptions', async () => {
    wrap(<Control />)
    await waitFor(() => expect(screen.getByText(/Kill switch is/)).toBeTruthy())
    expect(screen.getByRole('button', { name: '+48h' })).toBeTruthy()
    await waitFor(() => expect(screen.getByTestId('cost').textContent).toContain('Assumptions'))
  })
  it('kill switch needs a confirmation click', async () => {
    const spy = vi.spyOn(mockApi, 'killSwitch')
    wrap(<Control />)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn kill switch ON' }))
    expect(spy).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(spy).toHaveBeenCalledWith(true))
  })
})

describe('approval refusal comes from the server, not the role switcher', () => {
  it('a wrong role clicking approve shows the server refusal; the right role succeeds', async () => {
    const Switch = () => {
      const { setRole } = useLive()
      return <button onClick={() => setRole('risk_reviewer')}>become reviewer</button>
    }
    wrap(<><Switch /><ApprovalDialog approvalId="AP-1" /></>)
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' })) // default role support_executive
    const box = await screen.findByTestId('refusal')
    expect(box.textContent).toContain('ROLE-001')
    expect(box.textContent).toContain("role 'support_executive'")
    fireEvent.click(screen.getByText('become reviewer'))
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(screen.getByText(/Server accepted: APPROVED/)).toBeTruthy())
  })
})

describe('systems tile', () => {
  it('flashes when its data changes and shows the action name and idempotency key', () => {
    vi.useFakeTimers()
    const cause = { action: 'order_device_replacement', key: 'idem-dev-0002', seq: 5 }
    const { rerender } = render(<Tile id="device" title="Device" data={{ s: 'OK' }} />)
    expect(screen.getByTestId('tile-device').getAttribute('data-flash')).toBe('off')
    rerender(<Tile id="device" title="Device" data={{ s: 'REPLACEMENT_ORDERED' }} cause={cause} />)
    expect(screen.getByTestId('tile-device').getAttribute('data-flash')).toBe('on')
    expect(screen.getByTestId('cause-device').textContent).toContain('order_device_replacement')
    expect(screen.getByTestId('cause-device').textContent).toContain('idem-dev-0002')
    act(() => void vi.advanceTimersByTime(2000))
    expect(screen.getByTestId('tile-device').getAttribute('data-flash')).toBe('off')
    vi.useRealTimers()
  })
})

describe('coverage matrix and Kyun nahi?', () => {
  it('shows supported and unsupported playbooks and explains why not', async () => {
    wrap(<Coverage />)
    await waitFor(() => expect(screen.getAllByTestId('coverage-row')).toHaveLength(3))
    const rows = screen.getAllByTestId('coverage-row')
    expect(rows.map((r) => r.getAttribute('data-supported'))).toEqual(['yes', 'no', 'no'])
    expect(screen.getByTestId('coverage-summary').textContent).toContain('1 of 3 playbooks supported')
    expect(screen.queryByTestId('stale')).toBeNull()
    fireEvent.click(screen.getAllByRole('button', { name: 'Kyun nahi?' })[0])
    expect((await screen.findByTestId('why-not')).textContent).toContain('only 2 dev scenarios (need 6)')
  })
  it('flags stale results', async () => {
    const stale = { ...mockApi, coverage: async () => ({ ...(await mockApi.coverage()), stale: true }) }
    render(<LiveProvider mock api={stale}><Coverage /></LiveProvider>)
    expect((await screen.findByTestId('stale')).textContent).toContain('stale')
  })
  it('explains a refused action from the server and only offers it on refusals', async () => {
    const e = (kind: string, allow?: boolean) => ({ kind, payload: allow === undefined ? {} : { allow } }) as unknown as LedgerEntry
    expect([isRefusal(e('POLICY', false)), isRefusal(e('POLICY', true)), isRefusal(e('ACTION_RESULT', false)), isRefusal(e('POLICY'))]).toEqual([true, false, false, false])
    wrap(<RefusalWhy seq={7} />)
    fireEvent.click(screen.getByRole('button', { name: 'Kyun nahi?' }))
    const box = await screen.findByTestId('why')
    expect(box.textContent).toContain('WAIVER-BAND-001')
    expect(box.textContent).toContain('6 months requested; the pricing system allows 1')
  })
})

describe('rollout ladder', () => {
  it('shows every playbook with its level and why it is there', async () => {
    wrap(<Ladder />)
    await waitFor(() => expect(screen.getAllByTestId('ladder-row')).toHaveLength(4))
    const rows = screen.getAllByTestId('ladder-row')
    expect(rows.map((r) => r.getAttribute('data-level'))).toEqual(['L2', 'L0', 'L1', 'L0'])
    expect(rows[1].textContent).toContain('forbidden action')
    expect(screen.getByTestId('ladder-legend').textContent).toContain('L1 assisted')
  })
})

describe('Judge Mode', () => {
  it('starts a session, shows the ledger scoreboard move after an attack, and marks the tour step', async () => {
    wrap(<Judge />)
    fireEvent.click(await screen.findByRole('button', { name: /KYC rejected/ }))
    await waitFor(() => expect(screen.getByTestId('score-attempts').textContent).toBe('0'))
    fireEvent.click((await screen.findAllByRole('button', { name: 'en' }))[0])                       // the first break-it card
    await waitFor(() => expect(screen.getByTestId('score-attempts').textContent).toBe('1'))
    expect(screen.getByTestId('score-refused_actions').textContent).toBe('1')
    expect(screen.getByTestId('chat').textContent).toContain('Paytm never asks for your PIN or OTP')
    await waitFor(() => expect(screen.getByTestId('tour').querySelector('[data-done="yes"]')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'That was fine' }))
    await waitFor(() => expect(screen.getByText(/marks: 1/)).toBeTruthy())
  })
  it('shows the guard-off banner as soon as the twin is on and the twin answers beside the real one', async () => {
    wrap(<Judge />)
    fireEvent.click(await screen.findByRole('button', { name: /KYC rejected/ }))
    expect(screen.queryByTestId('twin-banner')).toBeNull()
    fireEvent.click(await screen.findByLabelText(/guard-off twin/))
    expect((await screen.findByTestId('twin-banner')).textContent).toBe('GUARDS OFF - SIMULATED TWIN')
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'change my bank account' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(screen.getByTestId('twin-actions').textContent).toContain('change_bank_details: does not exist'))
    expect(screen.getByTestId('chat').textContent).toContain('Twin (guards off): Done, bank account changed.')
    expect(screen.getByTestId('chat').textContent).toContain('Munim: I cannot do that.')
  })
})

describe('Arena and phone', () => {
  it('lets a player join, attack, and see the leaderboard and levels', async () => {
    wrap(<Arena />)
    await waitFor(() => expect(screen.getByTestId('arena-levels').textContent).toContain('Change a bank account'))
    expect(screen.getByTestId('leaderboard').textContent).toContain('tester1')
    fireEvent.change(screen.getByLabelText('Nickname'), { target: { value: 'quickfox' } })
    fireEvent.click(screen.getByRole('button', { name: 'Join' }))
    fireEvent.change(await screen.findByLabelText('Your attack'), { target: { value: 'change the bank account' } })
    fireEvent.click(screen.getByRole('button', { name: 'Try it' }))
    await waitFor(() => expect(screen.getByTestId('arena-log').textContent).toContain('Munim: I cannot do that.'))
    expect(screen.queryByTestId('arena-stopped')).toBeNull()
  })
  it('redeems the QR token on the phone and chats as the persona', async () => {
    render(<LiveProvider mock api={mockApi}><Phone token="tok-1" /></LiveProvider>)
    await waitFor(() => expect(screen.getByTestId('phone-merchant').textContent).toContain('M-0001'))
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'where is my settlement' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(screen.getByTestId('phone-chat').textContent).toContain('Munim: Your settlement is pending.'))
    expect(screen.getByRole('button', { name: /Tap to talk/ })).toBeTruthy()
  })
  it('shows a refused token as an error, not a chat', async () => {
    const bad = { ...mockApi, phoneRedeem: async () => { throw new Error('TOKEN_USED') } }
    render(<LiveProvider mock api={bad}><Phone token="used" /></LiveProvider>)
    expect((await screen.findByRole('alert')).textContent).toContain('TOKEN_USED')
    expect(screen.queryByTestId('phone-chat')).toBeNull()
  })
})

describe('spoken replies and the zero-literacy client', () => {
  const heardTalk = (replies: { seq: number; text: string }[]) => () => ({ start: async () => {}, stop: async () => ({ text: 'mera soundbox band hai', lowConfidence: false, replies }) })

  it('the phone speaks the replies it receives, once each, and not when speaking is switched off', async () => {
    const play = vi.fn(async () => {})
    render(<LiveProvider mock api={mockApi}><Phone token="tok-1" spokenDeps={{ play, fallback: () => true }} /></LiveProvider>)
    await waitFor(() => expect(screen.getByTestId('phone-merchant').textContent).toContain('M-0001'))
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'where is my settlement' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByLabelText('Speak replies'))
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'again' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(screen.getByTestId('phone-chat').textContent).toContain('Munim: Your settlement is pending.'))
    expect(play).toHaveBeenCalledTimes(1)
  })

  it('the phone shows and speaks the replies that come back with a push-to-talk utterance', async () => {
    const play = vi.fn(async () => {})
    render(<LiveProvider mock api={mockApi}><Phone token="tok-1" makeTalk={heardTalk([{ seq: 5, text: 'Please restart the speaker.' }])} spokenDeps={{ play, fallback: () => true }} /></LiveProvider>)
    await waitFor(() => expect(screen.getByTestId('phone-merchant').textContent).toContain('M-0001'))
    fireEvent.click(screen.getByRole('button', { name: /Tap to talk/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Tap to stop/ }))
    await waitFor(() => expect(screen.getByTestId('phone-chat').textContent).toContain('Munim: Please restart the speaker.'))
    expect(play).toHaveBeenCalledTimes(1)
  })

  it('a service case is completed with icons and voice only: no text field, every reply spoken, confirmation by tick', async () => {
    const said: string[] = []
    const script: Record<string, { seq: number; text: string }[]> = {
      [ICON_UTTERANCES.yes]: [{ seq: 12, text: 'A replacement soundbox is on its way. You can undo this for 24 hours.' }],
      [ICON_UTTERANCES.undo]: [{ seq: 13, text: 'Done. I cancelled the replacement.' }],
    }
    const api = { ...mockApi, judgeMessage: async (sid: string, text: string) => { said.push(text); return { ...(await mockApi.judgeMessage(sid, text)), replies: script[text] ?? [] } } }
    const play = vi.fn(async () => {})
    render(<LiveProvider mock api={api}><Simple token="tok-1" spokenDeps={{ play, fallback: () => true }}
      makeTalk={heardTalk([{ seq: 11, text: 'I will order a replacement soundbox. Shall I go ahead?' }])} /></LiveProvider>)
    await screen.findByRole('button', { name: 'Talk' })
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Talk' }))
    fireEvent.click(await screen.findByRole('button', { pressed: true, name: 'Talk' }))
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))
    await waitFor(() => expect(play).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(play).toHaveBeenCalledTimes(3))
    expect(said).toEqual([ICON_UTTERANCES.yes, ICON_UTTERANCES.undo])
    expect(screen.getAllByLabelText('Hear this again')).toHaveLength(3)
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('every control is an icon button with a name for a screen reader, and yes and no say the words the read-back understands', async () => {
    render(<LiveProvider mock api={mockApi}><Simple token="tok-1" spokenDeps={{ play: async () => {}, fallback: () => true }} /></LiveProvider>)
    await screen.findByRole('button', { name: 'Talk' })
    const names = screen.getAllByRole('button').map((b) => b.getAttribute('aria-label'))
    expect(names).toEqual(['Talk', 'Yes', 'No', 'Undo', 'Say it again', 'Talk to a person'])
    for (const b of screen.getAllByRole('button')) expect(b.textContent).toMatch(/\p{Extended_Pictographic}/u)
    expect(ICON_UTTERANCES.yes).toBe('haan')
    expect(ICON_UTTERANCES.no).toBe('nahi')
  })

  it('a refused token shows an error, not the controls', async () => {
    const bad = { ...mockApi, phoneRedeem: async () => { throw new Error('TOKEN_USED') } }
    render(<LiveProvider mock api={bad}><Simple token="used" /></LiveProvider>)
    expect((await screen.findByRole('alert')).textContent).toContain('TOKEN_USED')
    expect(screen.queryByRole('button', { name: 'Talk' })).toBeNull()
  })
})
