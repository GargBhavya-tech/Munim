import { useEffect, useRef, useState } from 'react'
import { ScoreboardPanel } from '../judge/ScoreboardPanel'
import type { JudgeView } from '../judge/api'
import { judgeApi, redeem, sendVoice } from './api'
import { encodeWav } from './wav'

type Line = { who: 'you' | 'munim'; text: string }

/** Mobile-first judge client: text box, hold-to-talk, scoreboard. Everything shown comes from the server. */
export function PhoneClient({ token, makeSocket, record }: {
  token: string
  makeSocket?: () => WebSocket
  record?: () => Promise<{ stop: () => Promise<{ samples: Float32Array; rate: number }> }>
}) {
  const [view, setView] = useState<JudgeView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lines, setLines] = useState<Line[]>([])
  const [text, setText] = useState('')
  const [talking, setTalking] = useState(false)
  const ws = useRef<WebSocket | null>(null)
  const rec = useRef<Awaited<ReturnType<NonNullable<typeof record>>> | null>(null)
  const started = useRef(false)

  useEffect(() => {
    if (started.current) return           // StrictMode runs effects twice; the token is single use, so redeem once
    started.current = true
    redeem(token).then((r) => (r.ok ? setView(r.view) : setError(r.error)))
  }, [token])

  const add = (l: Line) => setLines((x) => [...x, l])
  const send = async () => {
    if (!view || !text.trim()) return
    const t = text.trim()
    setText(''); add({ who: 'you', text: t })
    const r = await judgeApi.send(view.session.session_id, t)
    if (!r.ok) return add({ who: 'munim', text: r.refusal.reason })
    r.replies.forEach((x) => add({ who: 'munim', text: x.text }))
    setView(r.view)
  }
  const refresh = async () => { if (view) { const v = await fetch(`/api/judge/sessions/${view.session.session_id}`).then((r) => r.json()); setView(v) } }
  const down = async () => {
    if (!view || !record) return
    setTalking(true)
    rec.current = await record()
  }
  const up = async () => {
    if (!rec.current || !view) return
    const { samples, rate } = await rec.current.stop()
    rec.current = null; setTalking(false)
    const sock = (ws.current ??= (makeSocket ?? (() => new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/voice`)))())
    sock.onmessage = (m) => {
      const d = JSON.parse(m.data)
      add({ who: 'you', text: d.type === 'transcript' ? d.text : 'Could not hear that. Please repeat.' })
      void refresh()
    }
    sendVoice(sock, view.session.merchant_id, encodeWav(samples, rate))
  }

  if (error) return <main className="p-4"><p role="alert" data-testid="phone-error" className="font-bold text-red-800">{error}</p></main>
  if (!view) return <main className="p-4"><p>Opening session...</p></main>
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col gap-3 p-4" data-testid="phone-client">
      <p className="rounded bg-amber-200 px-2 py-1 text-center text-xs font-bold uppercase" data-testid="phone-simulated">SIMULATED · persona {view.session.persona_id}</p>
      <ul className="flex flex-1 flex-col gap-2" aria-live="polite">
        {lines.map((l, i) => <li key={i} data-who={l.who} className={`rounded p-2 ${l.who === 'you' ? 'self-end bg-slate-200' : 'self-start bg-emerald-100'}`}>{l.text}</li>)}
      </ul>
      <div className="flex gap-2">
        <input aria-label="Message" value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && void send()} className="min-w-0 flex-1 rounded border p-2 text-base" />
        <button type="button" onClick={() => void send()} className="rounded border px-3">Send</button>
      </div>
      <button type="button" data-testid="hold-to-talk" data-talking={talking} onPointerDown={() => void down()} onPointerUp={() => void up()} onPointerLeave={() => talking && void up()}
        className={`rounded p-4 text-lg font-bold ${talking ? 'bg-red-700 text-white' : 'bg-slate-800 text-white'}`}>
        {talking ? 'Release to send' : 'Hold to talk'}
      </button>
      <ScoreboardPanel board={view.scoreboard} usage={view.usage} caps={view.caps} />
    </main>
  )
}

/** Browser microphone capture to mono float samples. Not exercised in unit tests (needs a real device). */
export async function recordMic() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  const ctx = new AudioContext()
  const src = ctx.createMediaStreamSource(stream)
  const proc = ctx.createScriptProcessor(4096, 1, 1)
  const chunks: Float32Array[] = []
  proc.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)))
  src.connect(proc); proc.connect(ctx.destination)
  return {
    stop: async () => {
      proc.disconnect(); src.disconnect(); stream.getTracks().forEach((t) => t.stop())
      const n = chunks.reduce((a, c) => a + c.length, 0)
      const samples = new Float32Array(n); let o = 0
      chunks.forEach((c) => { samples.set(c, o); o += c.length })
      const rate = ctx.sampleRate; await ctx.close()
      return { samples, rate }
    },
  }
}
