import { useEffect, useRef, useState } from 'react'
import { useLive } from '../hooks/live'
import { redeemOnce } from '../lib/redeem'
import { ErrorNote, SimulatedBadge } from '../components/common'
import type { JudgeView, Reply } from '../api/types'
import { wsTalk, type Talk } from '../lib/pushToTalk'
import { speakAll, speakReply, voiceFor, type SpokenDeps } from '../lib/spoken'

/** What each icon says on the merchant's behalf. These are words the read-back, undo and hand-off steps already understand (core/voice/readback.py YES/NO,
 *  the merchant pack's UNDO_LAST and WANTS_HUMAN intents); nothing here is typed by the merchant. */
export const ICON_UTTERANCES = {
  yes: 'haan',
  no: 'nahi',
  undo: 'wapas karo',
  human: 'insaan se baat karni hai',
} as const

type Card = { reply: Reply }

const big = 'flex h-28 w-28 items-center justify-center rounded-3xl border-4 text-6xl focus:outline-4 focus:outline-sky-600 disabled:opacity-40'

// Declared outside Simple so the buttons keep their identity (and keyboard focus) across renders.
function Icon({ label, icon, onClick, tone, pressed, disabled }: { label: string; icon: string; onClick: () => void; tone: string; pressed?: boolean; disabled?: boolean }) {
  return <button type="button" aria-label={label} aria-pressed={pressed} disabled={disabled} onClick={onClick} className={`${big} ${tone}`}><span aria-hidden="true">{icon}</span></button>
}

/** Zero-literacy client (#122): voice and icons only. Every reply is spoken the moment it arrives; every answer the merchant might need to give (yes, no, undo,
 *  a person, say it again, talk) is one large icon button. There is no text field and no step that needs reading, including confirmations: a confirmation is
 *  a spoken question with a tick and a cross. The text of a reply is shown small for a helper, never required. */
export function Simple({ token, makeTalk = wsTalk, spokenDeps }: { token: string; makeTalk?: (merchantId: string) => Talk; spokenDeps?: SpokenDeps }) {
  const { api } = useLive()
  const [view, setView] = useState<JudgeView | null>(null)
  const [cards, setCards] = useState<Card[]>([])
  const [recording, setRecording] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const talk = useRef<Talk | null>(null)
  const fail = (e: unknown) => setErr(e instanceof Error ? e.message : String(e))

  useEffect(() => { redeemOnce(api, token).then(setView).catch(fail) }, [api, token])

  const hear = async (replies: Reply[]) => {
    setCards((c) => [...c, ...replies.map((reply) => ({ reply }))])
    if (view) await speakAll(voiceFor(api, view.session.session_id), replies, spokenDeps)
  }

  const say = async (utterance: string) => {
    if (!view || busy) return
    setBusy(true)
    try {
      const r = await api.judgeMessage(view.session.session_id, utterance)
      setView(r)
      await hear(r.replies ?? [])
    } catch (e) { fail(e) } finally { setBusy(false) }
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
      setBusy(true)
      const heard = await talk.current!.stop()
      await hear(heard.replies)
    } catch (e) { fail(e); setRecording(false) } finally { setBusy(false) }
  }

  const last = cards.length ? cards[cards.length - 1].reply : null

  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-4 p-4" data-testid="simple">
      <SimulatedBadge />
      <ErrorNote error={err} />
      {!view && !err && <p aria-label="Opening" className="text-5xl">⏳</p>}
      {view && (
        <>
          <div className="flex w-full flex-col gap-2" data-testid="simple-cards">
            {cards.map((c, i) => (
              <div key={i} className="flex items-center gap-3 rounded-2xl border-2 border-slate-300 bg-white p-3">
                <button type="button" aria-label="Hear this again" onClick={() => view && void speakReply(voiceFor(api, view.session.session_id), c.reply, spokenDeps)} className="h-16 w-16 rounded-full bg-sky-100 text-4xl"><span aria-hidden="true">🔊</span></button>
                <p className="text-xs text-slate-500">{c.reply.text}</p>
              </div>
            ))}
          </div>
          <div className="grid grid-cols-3 gap-4">
            <Icon label="Talk" icon={recording ? '⏹️' : '🎤'} pressed={recording} disabled={busy && !recording} onClick={() => void tap()} tone="border-sky-700 bg-sky-100" />
            <Icon label="Yes" icon="✅" disabled={busy || recording} onClick={() => void say(ICON_UTTERANCES.yes)} tone="border-green-700 bg-green-100" />
            <Icon label="No" icon="❌" disabled={busy || recording} onClick={() => void say(ICON_UTTERANCES.no)} tone="border-red-700 bg-red-100" />
            <Icon label="Undo" icon="↩️" disabled={busy || recording} onClick={() => void say(ICON_UTTERANCES.undo)} tone="border-amber-700 bg-amber-100" />
            <Icon label="Say it again" icon="🔁" disabled={!last || recording} onClick={() => last && view && void speakReply(voiceFor(api, view.session.session_id), last, spokenDeps)} tone="border-slate-600 bg-slate-100" />
            <Icon label="Talk to a person" icon="👤" disabled={busy || recording} onClick={() => void say(ICON_UTTERANCES.human)} tone="border-violet-700 bg-violet-100" />
          </div>
        </>
      )}
    </div>
  )
}
