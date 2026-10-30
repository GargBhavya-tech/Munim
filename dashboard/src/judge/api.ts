import type { BreakItCardData, CapRefusal, JudgeCaps, JudgeUsage, Scoreboard } from './types'

export interface JudgeView { session: { session_id: string; persona_id: string; merchant_id: string; start_seq: number }; scoreboard: Scoreboard; usage: JudgeUsage; caps: JudgeCaps }
export type SendResult = { ok: true; replies: { case_id: string; language: string; text: string }[]; view: JudgeView } | { ok: false; refusal: CapRefusal }

async function post<T>(path: string, body: unknown): Promise<{ status: number; data: T }> {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  return { status: res.status, data: (await res.json()) as T }
}

export const judgeApi = {
  async start(personaId: string): Promise<JudgeView> {
    return (await post<JudgeView>('/api/judge/sessions', { persona_id: personaId })).data
  },
  async send(sid: string, text: string, language?: string): Promise<SendResult> {
    const { status, data } = await post<any>(`/api/judge/sessions/${sid}/messages`, { text, language })
    if (status === 429) return { ok: false, refusal: data as CapRefusal }
    return { ok: true, replies: data.replies, view: { session: data.session, scoreboard: data.scoreboard, usage: data.usage, caps: data.caps } }
  },
  async fine(sid: string, refSeq?: number): Promise<Scoreboard> {
    return (await post<{ scoreboard: Scoreboard }>(`/api/judge/sessions/${sid}/feedback`, { verdict: 'fine', ref_seq: refSeq ?? null })).data.scoreboard
  },
  async breakIt(): Promise<{ cards: BreakItCardData[] }> {
    return (await (await fetch('/api/judge/break-it')).json()) as { cards: BreakItCardData[] }
  },
}
