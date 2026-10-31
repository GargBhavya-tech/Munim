import { useEffect, useRef, useState } from 'react'
import { useLive } from '@old/hooks/live'
import { redeemOnce } from '@old/lib/redeem'
import { wsTalk, type Talk } from '@old/lib/pushToTalk'
import { speakAll, speakReply, voiceFor } from '@old/lib/spoken'
import type { JudgeView, Reply } from '@old/api/types'
import { Note } from '../parts'
import { Stamp, SurfaceBox, Waveform } from '../ui'

/** The zero-literacy client: voice and large buttons only, no text field, no step that needs reading. Every reply is spoken as it arrives. A confirmation is a spoken
 *  question with a tick and a cross. The text of a reply is shown small, for a helper, and is never required. Open as #/simple/<token>. */

/** What each button says on the merchant's behalf: words the read-back, undo and hand-off steps already understand. */
const SAYS = { yes: 'haan', no: 'nahi', undo: 'wapas karo', human: 'insaan se baat karni hai' } as const

const big = 'flex h-28 w-full items-center justify-center rounded-[20px] border-2 text-5xl font-semibold transition-transform duration-150 active:scale-[0.97] disabled:opacity-40 focus-visible:outline-4'

function Key({ label, glyph, onClick, tone, pressed, disabled }: { label: string; glyph: string; onClick: () => void; tone: string; pressed?: boolean; disabled?: boolean }) {
  return <button type="button" aria-label={label} aria-pressed={pressed} disabled={disabled} onClick={onClick} className={`${big} ${tone}`}><span aria-hidden="true">{glyph}</span></button>
}

export function Simple({ token }: { token: string }) {
  const { api } = useLive()
  const [view, setView] = useState<JudgeView | null>(null)
  const [cards, setCards] = useState<Reply[]>([])
  const [recording, setRecording] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const talk = useRef<Talk | null>(null)
  const fail = (e: unknown) => setErr(e instanceof Error ? e.message : String(e))

  useEffect(() => { redeemOnce(api, token).then(setView).catch(fail) }, [api, token])

  const hear = async (replies: Reply[]) => {
    setCards((c) => [...c, ...replies])
    if (view) await speakAll(voiceFor(api, view.session.session_id), replies)
  }
  const say = async (utterance: string) => {
    if (!view || busy) return
    setBusy(true)
    try { const r = await api.judgeMessage(view.session.session_id, utterance); setView(r); await hear(r.replies ?? []) } catch (e) { fail(e) } finally { setBusy(false) }
  }
  const tap = async () => {
    if (!view) return
    try {
      if (!recording) { talk.current = wsTalk(view.session.merchant_id); await talk.current.start(); setRecording(true); return }
      setRecording(false)
      setBusy(true)
      const heard = await talk.current!.stop()
      await hear(heard.replies)
    } catch (e) { fail(e); setRecording(false) } finally { setBusy(false) }
  }
  const last = cards.length ? cards[cards.length - 1] : null
  const again = (r: Reply) => view && void speakReply(voiceFor(api, view.session.session_id), r)

  return (
    <SurfaceBox surface="paper" className="min-h-screen">
      <div className="mx-auto flex max-w-md flex-col gap-5 px-5 py-6" data-testid="simple">
        <div className="flex items-center justify-between"><span className="text-h3">Munim</span><Stamp tone="muted">simulated</Stamp></div>
        <Note error={err} />
        {!view && !err && <p aria-label="Opening" className="text-muted">Opening…</p>}
        {view && (
          <>
            <div className="ruled space-y-3 py-2" aria-live="polite">
              {cards.length === 0 && <p className="text-muted">Press the microphone and speak.</p>}
              {cards.map((r, i) => (
                <div key={i} className="flex items-center gap-3">
                  <button type="button" aria-label="Hear this again" onClick={() => again(r)} className="h-14 w-14 shrink-0 rounded-full border-2 border-line text-2xl"><span aria-hidden="true">▶</span></button>
                  <p className="text-small text-muted">{r.text}</p>
                </div>
              ))}
            </div>
            <div className="text-ink"><Waveform live={recording} bars={24} /></div>
            <div className="grid grid-cols-2 gap-4">
              <Key label="Talk" glyph={recording ? '■' : '●'} pressed={recording} disabled={busy && !recording} onClick={() => void tap()} tone="border-stamp bg-stamp text-white" />
              <Key label="Say it again" glyph="↻" disabled={!last || recording} onClick={() => last && again(last)} tone="border-line bg-surface" />
              <Key label="Yes" glyph="✓" disabled={busy || recording} onClick={() => void say(SAYS.yes)} tone="border-ok bg-surface text-ok" />
              <Key label="No" glyph="✕" disabled={busy || recording} onClick={() => void say(SAYS.no)} tone="border-bad bg-surface text-bad" />
              <Key label="Undo" glyph="↩" disabled={busy || recording} onClick={() => void say(SAYS.undo)} tone="border-line bg-surface" />
              <Key label="Talk to a person" glyph="☎" disabled={busy || recording} onClick={() => void say(SAYS.human)} tone="border-line bg-surface" />
            </div>
          </>
        )}
      </div>
    </SurfaceBox>
  )
}
