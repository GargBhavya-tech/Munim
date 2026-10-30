import { useEffect, useRef, useState } from 'react'
import { useLive } from '../hooks/live'
import { redeemOnce } from '../lib/redeem'
import { btnPrimary, ErrorNote, SimulatedBadge } from '../components/common'
import type { JudgeView, Reply } from '../api/types'
import { wsTalk, type Talk } from '../lib/pushToTalk'
import { speakAll, voiceFor, type SpokenDeps } from '../lib/spoken'

type Line = { who: 'you' | 'munim'; text: string }

/** The judge's phone (#130): opened from a single-use QR token, bound to one persona. Text chat plus push-to-talk, and Munim's replies are spoken. */
export function Phone({ token, makeTalk = wsTalk, spokenDeps }: { token: string; makeTalk?: (merchantId: string) => Talk; spokenDeps?: SpokenDeps }) {
  const { api } = useLive()
  const [view, setView] = useState<JudgeView | null>(null)
  const [lines, setLines] = useState<Line[]>([])
  const [text, setText] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [recording, setRecording] = useState(false)
  const [spoken, setSpoken] = useState(true)
  const talk = useRef<Talk | null>(null)
  const fail = (e: unknown) => setErr(e instanceof Error ? e.message : String(e))

  useEffect(() => { redeemOnce(api, token).then(setView).catch(fail) }, [api, token])

  const hear = (replies: Reply[]) => {
    setLines((l) => [...l, ...replies.map((x) => ({ who: 'munim' as const, text: x.text }))])
    if (spoken && view) void speakAll(voiceFor(api, view.session.session_id), replies, spokenDeps)
  }

  const send = async (msg: string) => {
    if (!view || !msg.trim()) return
    setLines((l) => [...l, { who: 'you', text: msg }])
    try {
      const r = await api.judgeMessage(view.session.session_id, msg)
      setView(r)
      hear(r.replies ?? [])
    } catch (e) { fail(e) }
  }

  const tap = async () => {
    if (!view) return
    try {
      if (!recording) {
        talk.current = makeTalk(view.session.merchant_id)
        await talk.current.start()
        setRecording(true)
        return
      }
      setRecording(false)
      const heard = await talk.current!.stop()
      setLines((l) => [...l, { who: 'you', text: `🎤 ${heard.text}${heard.lowConfidence ? ' (not sure I heard that right)' : ''}` }])
      hear(heard.replies)
    } catch (e) { fail(e); setRecording(false) }
  }

  return (
    <div className="mx-auto flex max-w-md flex-col gap-3 p-4">
      <h1 className="text-xl font-bold">Munim <SimulatedBadge /></h1>
      <ErrorNote error={err} />
      {!view && !err && <p>Opening your session...</p>}
      {view && (
        <>
          <p className="text-sm text-slate-600" data-testid="phone-merchant">You are talking as {view.session.merchant_id}</p>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={spoken} onChange={(e) => setSpoken(e.target.checked)} /> Speak replies</label>
          <div data-testid="phone-chat" className="flex min-h-48 flex-col gap-1">{lines.map((l, i) => <p key={i} className={l.who === 'you' ? 'text-right' : ''}><b>{l.who === 'you' ? 'You' : 'Munim'}:</b> {l.text}</p>)}</div>
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); const t = text; setText(''); void send(t) }}>
            <input aria-label="Message" className="flex-1 rounded border border-slate-400 px-2 py-2 text-lg" value={text} onChange={(e) => setText(e.target.value)} />
            <button type="submit" className={btnPrimary}>Send</button>
          </form>
          <button type="button" aria-pressed={recording} className={`${btnPrimary} py-4 text-lg`} onClick={() => void tap()}>{recording ? 'Tap to stop and send' : '🎤 Tap to talk'}</button>
        </>
      )}
    </div>
  )
}
