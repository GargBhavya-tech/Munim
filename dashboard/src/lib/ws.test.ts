import { describe, expect, it } from 'vitest'
import { backoffMs, ReconnectingSocket, type ConnStatus, type SocketLike } from './ws'

class FakeSock implements SocketLike {
  onopen: SocketLike['onopen'] = null
  onclose: SocketLike['onclose'] = null
  onerror: SocketLike['onerror'] = null
  onmessage: SocketLike['onmessage'] = null
  sent: string[] = []
  closed = false
  send(d: string) { this.sent.push(d) }
  close() { this.closed = true }
}

function setup() {
  const socks: FakeSock[] = []
  const timers: { fn: () => void; ms: number }[] = []
  const statuses: ConnStatus[] = []
  const msgs: unknown[] = []
  const rs = new ReconnectingSocket({
    url: 'ws://x/ws',
    factory: () => { const s = new FakeSock(); socks.push(s); return s },
    onMessage: (m) => msgs.push(m),
    onStatus: (s) => statuses.push(s),
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length },
    clearTimer: () => {},
  })
  return { rs, socks, timers, statuses, msgs }
}

describe('backoff', () => {
  it('doubles and caps', () => {
    expect([0, 1, 2, 3].map((a) => backoffMs(a))).toEqual([500, 1000, 2000, 4000])
    expect(backoffMs(20)).toBe(15000)
  })
})

describe('ReconnectingSocket', () => {
  it('reconnects with growing delay and resets after a successful open', () => {
    const { rs, socks, timers, statuses } = setup()
    rs.start()
    socks[0].onclose?.({})
    expect(timers[0].ms).toBe(500)
    timers[0].fn()
    socks[1].onclose?.({})
    expect(timers[1].ms).toBe(1000)
    timers[1].fn()
    socks[2].onopen?.({})
    expect(statuses.at(-1)).toBe('open')
    socks[2].onclose?.({})
    expect(timers[2].ms).toBe(500)
    expect(statuses.at(-1)).toBe('reconnecting')
  })
  it('delivers parsed messages, ignores junk, re-sends subscription on reconnect', () => {
    const { rs, socks, timers, msgs } = setup()
    rs.start()
    rs.subscribe('CS-0001')
    socks[0].onopen?.({})
    expect(socks[0].sent).toEqual([JSON.stringify({ subscribe: { case_id: 'CS-0001' } })])
    socks[0].onmessage?.({ data: '{"type":"hello","sim_time":"t"}' })
    socks[0].onmessage?.({ data: 'not json' })
    expect(msgs).toEqual([{ type: 'hello', sim_time: 't' }])
    socks[0].onclose?.({})
    timers[0].fn()
    socks[1].onopen?.({})
    expect(socks[1].sent).toHaveLength(1)
  })
  it('does not reconnect after stop()', () => {
    const { rs, socks, timers } = setup()
    rs.start()
    rs.stop()
    socks[0].onclose?.({})
    expect(timers).toHaveLength(0)
    expect(socks[0].closed).toBe(true)
  })
})
