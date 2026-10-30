import { useEffect, useState } from 'react'
import { useLive } from '../hooks/live'
import { btn, btnPrimary, ErrorNote, Panel } from '../components/common'
import type { BreakItCard, JudgeView, PersonaCard, Reply, TourProgress, TourStep, TwinReply } from '../api/types'

const TILES: [keyof JudgeView['scoreboard'], string][] = [
  ['attempts', 'Attempts'], ['blocked_claims', 'Blocked claims'], ['refused_actions', 'Refused actions'], ['escalations', 'Escalations'], ['forbidden_executed', 'Forbidden executed'],
]

type Line = { who: 'you' | 'munim' | 'twin'; text: string }

export function Judge() {
  const { api } = useLive()
  const [personas, setPersonas] = useState<PersonaCard[]>([])
  const [cards, setCards] = useState<BreakItCard[]>([])
  const [steps, setSteps] = useState<TourStep[]>([])
  const [view, setView] = useState<JudgeView | null>(null)
  const [tour, setTour] = useState<TourProgress | null>(null)
  const [lines, setLines] = useState<Line[]>([])
  const [text, setText] = useState('')
  const [twinOn, setTwinOn] = useState(false)
  const [twinBanner, setTwinBanner] = useState<string | null>(null)
  const [twinActions, setTwinActions] = useState<string[]>([])
  const [err, setErr] = useState<string | null>(null)
  const fail = (e: unknown) => setErr(e instanceof Error ? e.message : String(e))

  useEffect(() => {
    api.personas().then((r) => setPersonas(r.personas)).catch(fail)
    api.breakIt().then((r) => setCards(r.cards)).catch(fail)
    api.tourSteps().then((r) => setSteps(r.steps)).catch(fail)
  }, [api])

  const refreshTour = (sid: string) => api.tourProgress(sid).then(setTour).catch(fail)
  const start = (p: PersonaCard) => api.judgeStart(p.persona_id).then((v) => { setView(v); setLines([]); setErr(null); void refreshTour(v.session.session_id) }).catch(fail)
  const add = (who: Line['who'], replies: Reply[]) => setLines((l) => [...l, ...replies.map((r) => ({ who, text: r.text }))])

  const send = async (override?: string) => {
    const msg = (override ?? text).trim()
    if (!view || !msg) return
    setText('')
    setLines((l) => [...l, { who: 'you', text: msg }])
    try {
      if (twinOn) {
        const r: TwinReply = await api.twinMessage(view.session.merchant_id, msg, view.session.session_id)
        if (r.real.session && r.real.scoreboard) setView({ ...view, ...r.real } as JudgeView)     // the real half counted as a judge attempt
        setTwinBanner(r.banner)
        add('munim', r.real.replies)
        add('twin', r.twin.replies)
        setTwinActions(r.twin.actions.map((a) => `${a.action} ${a.status}`).concat(r.twin.nonexistent_actions_requested.map((n) => `${n}: does not exist`)))
      } else {
        const r = await api.judgeMessage(view.session.session_id, msg)
        setView(r)
        add('munim', r.replies ?? [])
      }
      void refreshTour(view.session.session_id)
    } catch (e) { fail(e) }
  }

  const toggleTwin = async (on: boolean) => {
    setTwinOn(on)
    if (on) { const s = await api.twinStart().catch((e) => { fail(e); return null }); setTwinBanner(s?.banner ?? null) } else { setTwinBanner(null); setTwinActions([]) }
  }

  const sid = view?.session.session_id
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-bold">Judge Mode</h1>
      <ErrorNote error={err} />
      {twinBanner && <div role="alert" data-testid="twin-banner" className="rounded border-4 border-red-700 bg-red-100 p-2 text-center text-xl font-extrabold text-red-900">{twinBanner}</div>}
      {!view && (
        <Panel title="Pick a merchant to talk to">
          <ul className="flex flex-col gap-2">
            {personas.map((p) => <li key={p.persona_id}><button type="button" className={btn} onClick={() => start(p)}>{p.title}</button> <span className="text-sm text-slate-600">{p.description}</span></li>)}
          </ul>
        </Panel>
      )}
      {view && (
        <div className="grid gap-4 lg:grid-cols-3">
          <Panel title={`Conversation (${view.session.merchant_id})`} className="lg:col-span-2">
            <div data-testid="chat" className="mb-2 flex min-h-40 flex-col gap-1">
              {lines.map((l, i) => <p key={i} data-who={l.who} className={l.who === 'you' ? 'text-right' : l.who === 'twin' ? 'text-red-800' : ''}><b>{l.who === 'you' ? 'You' : l.who === 'twin' ? 'Twin (guards off)' : 'Munim'}:</b> {l.text}</p>)}
            </div>
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void send() }}>
              <input aria-label="Message" className="flex-1 rounded border border-slate-400 px-2 py-1" value={text} onChange={(e) => setText(e.target.value)} />
              <button type="submit" className={btnPrimary}>Send</button>
              <button type="button" className={btn} onClick={() => sid && api.judgeFine(sid).then((r) => setView({ ...view, scoreboard: r.scoreboard })).catch(fail)}>That was fine</button>
            </form>
            <label className="mt-2 flex items-center gap-2 text-sm"><input type="checkbox" checked={twinOn} onChange={(e) => void toggleTwin(e.target.checked)} /> Show the guard-off twin beside the real one</label>
            {twinActions.length > 0 && <p data-testid="twin-actions" className="mt-1 text-sm text-red-800">The twin did: {twinActions.join(', ')}</p>}
          </Panel>
          <div className="flex flex-col gap-4">
            <Panel title="Scoreboard (from the ledger)">
              <dl data-testid="scoreboard" className="grid grid-cols-2 gap-2">
                {TILES.map(([k, label]) => <div key={k} className="rounded bg-slate-100 p-2"><dt className="text-xs">{label}</dt><dd data-testid={`score-${k}`} className="text-2xl font-bold">{view.scoreboard[k]}</dd></div>)}
              </dl>
              <p className="mt-1 text-xs text-slate-600">"That was fine" marks: {view.scoreboard.fine_marks}. SIMULATED data.</p>
            </Panel>
            <Panel title="Try to break it">
              {cards.map((c) => <div key={c.id} className="mb-2"><b>{c.title}</b>{c.tries.map((t, i) => <button key={i} type="button" className={`${btn} ml-1 text-sm`} onClick={() => void send(t.text)}>{t.language}</button>)}</div>)}
            </Panel>
            <Panel title={`Tour ${tour ? `${tour.done}/${tour.total}` : ''}`}>
              <ol data-testid="tour" className="list-decimal pl-5">
                {steps.map((s) => { const done = tour?.steps.find((x) => x.id === s.id)?.done; return <li key={s.id} data-done={done ? 'yes' : 'no'}>{done ? '✓ ' : ''}{s.title}{tour?.next === s.id && <span className="text-sm text-slate-600"> — {s.instruction}</span>}</li> })}
              </ol>
            </Panel>
            <a className={`${btn} text-center`} href={`/api/export/judge/${encodeURIComponent(view.session.session_id)}?format=html`}>Download evidence pack</a>
          </div>
        </div>
      )}
    </div>
  )
}
