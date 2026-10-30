import { describe, expect, it, vi } from 'vitest'
import { speakAll, speakReply, voiceFor } from './spoken'

const deps = () => ({ play: vi.fn(async () => {}), fallback: vi.fn(() => true) })

describe('spoken replies', () => {
  it('voices a reply with a ledger seq from the server and not with the browser voice', async () => {
    const d = deps()
    const api = { speakReply: vi.fn(async () => new ArrayBuffer(4)) }
    expect(await speakReply(api, { seq: 7, text: 'Settlements status is PENDING.', language: 'hinglish' }, d)).toBe('server')
    expect(api.speakReply).toHaveBeenCalledWith(7)
    expect(d.play).toHaveBeenCalledTimes(1)
    expect(d.fallback).not.toHaveBeenCalled()
  })
  it('falls back to the browser voice when the server has no speech service or the audio will not play', async () => {
    const d = deps()
    const api = { speakReply: vi.fn(async () => { throw new Error('HTTP 503') }) }
    expect(await speakReply(api, { seq: 7, text: 'Hello', language: 'en-IN' }, d)).toBe('browser')
    expect(d.fallback).toHaveBeenCalledWith('Hello', 'en-IN')
    const d2 = { play: vi.fn(async () => { throw new Error('autoplay blocked') }), fallback: vi.fn(() => true) }
    expect(await speakReply({ speakReply: async () => new ArrayBuffer(1) }, { seq: 8, text: 'Hi' }, d2)).toBe('browser')
  })
  it('a reply with no seq is never sent to the server', async () => {
    const d = deps()
    const api = { speakReply: vi.fn(async () => new ArrayBuffer(1)) }
    await speakReply(api, { text: 'No ledger entry' }, d)
    expect(api.speakReply).not.toHaveBeenCalled()
    expect(d.fallback).toHaveBeenCalledWith('No ledger entry', 'en-IN')
  })
  it('reports none when nothing can speak', async () => {
    const d = { play: vi.fn(async () => {}), fallback: vi.fn(() => false) }
    expect(await speakReply({ speakReply: async () => { throw new Error('x') } }, { seq: 1, text: 'a' }, d)).toBe('none')
  })
  it('speaks several replies one after another, in order', async () => {
    const order: string[] = []
    const d = { play: vi.fn(async () => { order.push('play') }), fallback: vi.fn(() => true) }
    const api = { speakReply: async (seq: number) => { order.push('fetch' + seq); return new ArrayBuffer(1) } }
    await speakAll(api, [{ seq: 1, text: 'a' }, { seq: 2, text: 'b' }], d)
    expect(order).toEqual(['fetch1', 'play', 'fetch2', 'play'])
  })
})

describe('voiceFor', () => {
  it('sends the session id with every request, so the server voices only this session\'s messages', async () => {
    const api = { speakReply: vi.fn(async () => new ArrayBuffer(2)) }
    await speakReply(voiceFor(api, 'JS-41'), { seq: 9, text: 'Hello', language: 'en-IN' }, deps())
    expect(api.speakReply).toHaveBeenCalledWith(9, 'JS-41')
  })
})
