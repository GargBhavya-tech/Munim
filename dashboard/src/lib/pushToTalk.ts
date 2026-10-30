import type { Reply } from '../api/types'
import { startRecording } from './wav'

/** What the server heard, and the gate-checked replies the engine wrote for it (the /ws/voice transcript message). */
export interface Heard { text: string; lowConfidence: boolean; replies: Reply[] }
export interface Talk {
  start(): Promise<void>
  stop(): Promise<Heard>
}

type Recorder = { stop: () => Promise<ArrayBuffer> }

/** Push-to-talk over /ws/voice: record until stop(), send one WAV, resolve with the transcript and replies. Rejects with the server's "please repeat" prompt
 *  when the clip is dropped. The microphone path itself is not unit-tested (it needs a real device); everything around it is. */
export function wsTalk(merchantId: string, deps: { record?: () => Promise<Recorder>; makeSocket?: () => WebSocket } = {}): Talk {
  const record = deps.record ?? startRecording
  const makeSocket = deps.makeSocket ?? (() => new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/voice`))
  let rec: Recorder | null = null
  return {
    async start() {
      rec = await record()
    },
    async stop() {
      if (!rec) throw new Error('not recording')
      const wav = await rec.stop()
      rec = null
      return new Promise<Heard>((resolve, reject) => {
        const ws = makeSocket()
        ws.binaryType = 'arraybuffer'
        ws.onopen = () => { ws.send(JSON.stringify({ merchant_id: merchantId, language: 'unknown' })); ws.send(wav) }
        ws.onerror = () => reject(new Error('voice connection failed'))
        ws.onmessage = (m) => {
          const j = JSON.parse(String(m.data))
          ws.close()
          if (j.type === 'transcript') resolve({ text: j.text, lowConfidence: !!j.low_confidence, replies: j.replies ?? [] })
          else reject(new Error(j.repeat_prompt ?? 'Please say that again.'))
        }
      })
    },
  }
}
