import type { Reply } from '../api/types'
import { speak as browserSpeak } from './conversation'

/** Spoken replies. A reply is voiced by the SERVER (POST /api/voice/speak/{seq}, which only voices text the grounding gate let through) when it has a ledger
 *  seq, and by the browser's own voice otherwise or when the server has no speech service. It speaks the stored text; nothing is composed here. */
export interface SpokenApi { speakReply(seq: number): Promise<ArrayBuffer> }

/** The server voices only messages to the caller's own session (POST /api/voice/speak/{seq}?session_id=...): bind the session once. */
export const voiceFor = (api: { speakReply(seq: number, sessionId: string): Promise<ArrayBuffer> }, sessionId: string): SpokenApi =>
  ({ speakReply: (seq) => api.speakReply(seq, sessionId) })
export interface SpokenDeps {
  play: (audio: ArrayBuffer) => Promise<void>
  fallback: (text: string, language: string) => boolean
}

export function playWav(audio: ArrayBuffer): Promise<void> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(new Blob([audio], { type: 'audio/wav' }))
    const el = new Audio(url)
    const done = () => { URL.revokeObjectURL(url); resolve() }
    el.onended = done
    el.onerror = () => { URL.revokeObjectURL(url); reject(new Error('audio failed')) }
    el.play().catch((e) => { URL.revokeObjectURL(url); reject(e) })
  })
}

const defaults: SpokenDeps = { play: playWav, fallback: browserSpeak }

export type SpokenHow = 'server' | 'browser' | 'none'

export async function speakReply(api: SpokenApi, reply: Reply, deps: SpokenDeps = defaults): Promise<SpokenHow> {
  if (reply.seq != null) {
    try {
      await deps.play(await api.speakReply(reply.seq))
      return 'server'
    } catch {
      /* no speech service, or the audio would not play: use the browser's voice */
    }
  }
  return deps.fallback(reply.text, reply.language ?? 'en-IN') ? 'browser' : 'none'
}

/** One after another, in order, so replies never talk over each other. */
export async function speakAll(api: SpokenApi, replies: Reply[], deps?: SpokenDeps): Promise<SpokenHow[]> {
  const out: SpokenHow[] = []
  for (const r of replies) out.push(await speakReply(api, r, deps))
  return out
}
