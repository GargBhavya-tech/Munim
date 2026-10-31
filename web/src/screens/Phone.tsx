import { useEffect, useRef, useState } from 'react'
import { useLive } from '@old/hooks/live'
import { wsTalk, type Talk } from '@old/lib/pushToTalk'
import { redeemOnce } from '@old/lib/redeem'
import { speakAll, voiceFor } from '@old/lib/spoken'
import type { JudgeView, PersonaCard, Reply } from '@old/api/types'
import { ChatLog, Composer, useConversation, useMark, WhyDrawer } from '../chat'
import { Button, Stamp, SurfaceBox, Waveform } from '../ui'

/** What the shopkeeper sees: paper, ruled lines, Munim's words in serif with the stamp of their source, a mic to talk, and replies that are spoken aloud.
 *  Open as #/phone/<persona id> or from a judge's QR as #/phone/<token>. */

export function Phone({ arg }: { arg: string | null }) {
  const { api } = useLive()
  const [view, setView] = useState<JudgeView | null>(null)
  const [personas, setPersonas] = useState<PersonaCard[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [recording, setRecording] = useState(false)
  const [speaking, setSpeaking] = useState(false)
  const [spoken, setSpoken] = useState(true)
  const [why, setWhy] = useState<number | null>(null)
  const talk = useRef<Talk | null>(null)
  const since = useRef(0)
  const fail = (e: unknown) => setErr(e instanceof Error ? e.message : String(e))

  const onReplies = (replies: Reply[]) => {
    if (!spoken || !view) return
    setSpeaking(true)
    void speakAll(voiceFor(api, view.session.session_id), replies).finally(() => setSpeaking(false))
  }
  const conv = useConversation({ api, view, onView: setView, onReplies })
  const mark = useMark(view?.session.start_seq ?? 0)

  useEffect(() => {
    if (view) return
    if (arg?.startsWith('jt_')) redeemOnce(api, arg).then(setView).catch(fail)           // a judge's QR: single use, bound to one merchant
    else if (arg) api.judgeStart(arg).then(setView).catch(fail)
    else api.personas().then((r) => setPersonas(r.personas)).catch(fail)
  }, [api, arg])                                                                            // eslint-disable-line react-hooks/exhaustive-deps

  const tap = async () => {
    if (!view) return
    try {
      if (!recording) {
        since.current = mark()
        talk.current = wsTalk(view.session.merchant_id)
        await talk.current.start()
        setRecording(true)
        return
      }
      setRecording(false)
      const heard = await talk.current!.stop()
      conv.ingest(heard.text + (heard.lowConfidence ? ' (not sure I heard that right)' : ''), heard.replies, since.current)
      onReplies(heard.replies)
    } catch (e) { fail(e); setRecording(false) }
  }

  return (
    <div className="mx-auto flex max-w-5xl flex-col items-center gap-10 px-6 py-10 md:flex-row md:items-start md:justify-center">
      <div className="max-w-xs md:pt-10">
        <div className="text-label text-muted">The Counter</div>
        <h1 className="text-h1 mt-1">What the shopkeeper sees.</h1>
        <p className="text-body text-muted mt-4">Munim answers in the shopkeeper's own words. Under each answer, a stamp shows where every proven sentence came from. Talk to it, or type.</p>
        <label className="text-small mt-6 flex items-center gap-2 text-muted"><input type="checkbox" checked={spoken} onChange={(e) => setSpoken(e.target.checked)} /> Speak the replies aloud</label>
      </div>

      <SurfaceBox surface="paper" className="flex h-[min(78vh,46rem)] w-full max-w-sm flex-col rounded-[36px] border-2 border-line p-5">
        <div className="mb-3 flex items-center justify-between">
          <span className="text-label text-muted">{view ? view.session.merchant_id : 'choose a merchant'}</span>
          <Stamp tone="muted">simulated</Stamp>
        </div>
        {err && <p role="alert" className="text-bad text-small">{err}</p>}
        {!view && !arg && !err && (
          <ul className="space-y-2 overflow-y-auto">
            {personas.map((p) => <li key={p.persona_id}><button type="button" onClick={() => api.judgeStart(p.persona_id).then(setView).catch(fail)} className="min-h-11 w-full border border-line bg-surface px-3 py-2 text-left hover:border-fg"><span className="font-semibold">{p.title}</span><span className="text-small text-muted block">{p.merchant_id}</span></button></li>)}
          </ul>
        )}
        {!view && arg && !err && <p className="text-muted">Opening your session…</p>}
        {view && (
          <>
            <div className="ruled -mx-1 flex-1 overflow-y-auto px-1 py-2">
              <ChatLog turns={conv.turns} onWhy={setWhy} empty={<p className="text-muted">Say something, or tap the mic.</p>} />
            </div>
            {conv.error && <p role="alert" className="text-bad text-small mt-2">{conv.error}</p>}
            <div className="mt-3 flex items-center gap-3 text-ink">
              <Button aria-pressed={recording} variant={recording ? 'primary' : 'ghost'} onClick={() => void tap()} className="shrink-0">{recording ? 'Tap to stop and send' : 'Tap to talk'}</Button>
              <Waveform live={recording || speaking} bars={20} />
            </div>
            <Composer onSend={(t) => void conv.say(t)} busy={conv.busy} />
          </>
        )}
      </SurfaceBox>
      <WhyDrawer api={api} seq={why} onClose={() => setWhy(null)} />
    </div>
  )
}
