export interface LevelView { id: string; order: number; title: string; goal: string; broken?: boolean; attempt_no?: number | null }
export interface Row { rank: number; nickname: string; breaks: number; blocked: number; attempts: number }
export interface Info { label: string; state: 'RUNNING' | 'KILLED' | 'BUDGET_STOPPED'; levels: LevelView[]; leaderboard: Row[]; budget: { spent_paise: number; budget_paise: number } }
export interface AttemptResult { replies: { text: string }[]; attempt_no: number; cleared_now: string[]; levels: LevelView[]; state: Info['state'] }
export type Failure = { ok: false; status: number; error: string; reason: string }

async function call(path: string, body?: unknown) {
  const res = await fetch(path, body === undefined ? undefined : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  return { status: res.status, data: await res.json() }
}
const fail = (status: number, d: any): Failure => ({ ok: false, status, error: d?.detail?.error ?? 'ERROR', reason: d?.detail?.reason ?? '' })

export const arenaApi = {
  info: async (): Promise<Info> => (await call('/api/arena')).data,
  async join(nickname: string): Promise<{ ok: true; token: string; nickname: string } | Failure> {
    const { status, data } = await call('/api/arena/players', { nickname })
    return status === 200 ? { ok: true, token: data.token, nickname: data.nickname } : fail(status, data)
  },
  async attempt(token: string, text: string): Promise<{ ok: true; data: AttemptResult } | Failure> {
    const { status, data } = await call(`/api/arena/players/${token}/attempts`, { text })
    return status === 200 ? { ok: true, data } : fail(status, data)
  },
}
