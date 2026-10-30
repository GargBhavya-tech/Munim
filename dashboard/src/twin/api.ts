import type { FanOut } from './types'

async function post(path: string, body?: unknown): Promise<{ status: number; data: any }> {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
  return { status: res.status, data: await res.json() }
}

export const twinApi = {
  start: () => post('/api/twin/start'),
  async send(merchantId: string, text: string, language?: string): Promise<{ ok: true; data: FanOut } | { ok: false; status: number; error: string }> {
    const { status, data } = await post('/api/twin/messages', { merchant_id: merchantId, text, language })
    if (status !== 200) return { ok: false, status, error: data?.detail?.error ?? 'ERROR' }
    return { ok: true, data: data as FanOut }
  },
}
