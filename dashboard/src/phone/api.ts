import { judgeApi, type JudgeView } from '../judge/api'

export type RedeemResult = { ok: true; view: JudgeView } | { ok: false; error: string }

const MESSAGES: Record<string, string> = {
  UNKNOWN_TOKEN: 'This QR code is not valid.',
  TOKEN_USED: 'This QR code was already used. Ask for a new one.',
  TOKEN_EXPIRED: 'This QR code has expired. Ask for a new one.',
  PERSONA_MISMATCH: 'This QR code belongs to a different persona.',
}

/** Redeem the single-use QR token. The persona is decided by the server from the token, never by the phone. */
export async function redeem(token: string): Promise<RedeemResult> {
  const res = await fetch('/api/judge/phone/redeem', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) })
  const data = await res.json()
  if (res.ok) return { ok: true, view: data as JudgeView }
  const code: string = data?.detail?.error ?? 'UNKNOWN_TOKEN'
  return { ok: false, error: MESSAGES[code] ?? 'Could not open the session.' }
}

export { judgeApi }

/** One push-to-talk exchange over the existing voice websocket protocol: a setup text frame, then the WAV bytes. */
export function sendVoice(ws: WebSocket, merchantId: string, wav: Uint8Array<ArrayBuffer>, language = 'unknown') {
  ws.send(JSON.stringify({ merchant_id: merchantId, language, hinglish: false }))
  ws.send(wav)
}
