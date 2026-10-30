// Push-to-talk audio: the browser records raw float samples; the speech-to-text API wants a 16-bit PCM WAV, mono, 16 kHz.
export const TARGET_RATE = 16000

/** Average-pool downsample from `rate` to TARGET_RATE (no-op if already lower or equal). */
export function downsample(samples: Float32Array, rate: number): Float32Array {
  if (rate <= TARGET_RATE) return samples
  const ratio = rate / TARGET_RATE
  const out = new Float32Array(Math.floor(samples.length / ratio))
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio)
    const end = Math.min(samples.length, Math.floor((i + 1) * ratio))
    let sum = 0
    for (let j = start; j < end; j++) sum += samples[j]
    out[i] = sum / Math.max(1, end - start)
  }
  return out
}

export function encodeWav(samples: Float32Array, rate: number): ArrayBuffer {
  const pcm = downsample(samples, rate)
  const buf = new ArrayBuffer(44 + pcm.length * 2)
  const v = new DataView(buf)
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)) }
  str(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); str(8, 'WAVE'); str(12, 'fmt ')
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true)
  v.setUint32(24, TARGET_RATE, true); v.setUint32(28, TARGET_RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true)
  str(36, 'data'); v.setUint32(40, pcm.length * 2, true)
  for (let i = 0; i < pcm.length; i++) {
    const s = Math.max(-1, Math.min(1, pcm[i]))
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return buf
}

/** Records the microphone until stop() is called; resolves with a WAV. Not unit-tested (needs a real device). */
export async function startRecording(): Promise<{ stop: () => Promise<ArrayBuffer> }> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  const ctx = new AudioContext()
  const src = ctx.createMediaStreamSource(stream)
  const proc = ctx.createScriptProcessor(4096, 1, 1)
  const chunks: Float32Array[] = []
  proc.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)))
  src.connect(proc)
  proc.connect(ctx.destination)
  return {
    stop: async () => {
      proc.disconnect(); src.disconnect(); stream.getTracks().forEach((t) => t.stop())
      const all = new Float32Array(chunks.reduce((n, c) => n + c.length, 0))
      let o = 0
      for (const c of chunks) { all.set(c, o); o += c.length }
      const rate = ctx.sampleRate
      await ctx.close()
      return encodeWav(all, rate)
    },
  }
}
