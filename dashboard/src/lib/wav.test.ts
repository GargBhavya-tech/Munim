import { describe, expect, it } from 'vitest'
import { downsample, encodeWav, TARGET_RATE } from './wav'

const ascii = (v: DataView, o: number, n: number) => Array.from({ length: n }, (_, i) => String.fromCharCode(v.getUint8(o + i))).join('')

describe('wav encoder', () => {
  it('writes a 16 kHz mono 16-bit PCM header and clamps samples', () => {
    const buf = encodeWav(new Float32Array([0, 0.5, -0.5, 2, -2]), TARGET_RATE)
    const v = new DataView(buf)
    expect(ascii(v, 0, 4)).toBe('RIFF')
    expect(ascii(v, 8, 4)).toBe('WAVE')
    expect(ascii(v, 36, 4)).toBe('data')
    expect(v.getUint16(20, true)).toBe(1)
    expect(v.getUint16(22, true)).toBe(1)
    expect(v.getUint32(24, true)).toBe(16000)
    expect(v.getUint16(34, true)).toBe(16)
    expect(v.getUint32(40, true)).toBe(10)
    expect(buf.byteLength).toBe(44 + 10)
    expect([0, 1, 2, 3, 4].map((i) => v.getInt16(44 + i * 2, true))).toEqual([0, 16383, -16384, 32767, -32768])
  })
  it('downsamples 48 kHz to 16 kHz by averaging and leaves lower rates alone', () => {
    const out = downsample(new Float32Array([1, 1, 1, 0, 0, 0]), 48000)
    expect(Array.from(out)).toEqual([1, 0])
    const same = new Float32Array([0.1, 0.2])
    expect(downsample(same, 16000)).toBe(same)
    expect(encodeWav(new Float32Array(48000), 48000).byteLength).toBe(44 + 16000 * 2)
  })
})
