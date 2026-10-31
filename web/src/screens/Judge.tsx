import { motion } from 'motion/react'
import { useEffect, useState } from 'react'
import { useLive } from '@old/hooks/live'
import type { BreakItCard, JudgeView, PersonaCard, TourProgress, TourStep } from '@old/api/types'
import { ChatLog, Composer, useConversation, WhyDrawer } from '../chat'
import { ease, STAGGER, useMotion } from '../motion'
import { Button, Readout, Reveal, Stamp, SurfaceBox, Tag } from '../ui'

/** Judge Mode: the screen a judge uses. They pick a merchant, talk to Munim on the production path, try to break it, and watch the scoreboard (counted from the
 *  ledger, never from what Munim said). The guard-off twin shows what the same message does with every guard removed. */

const LANG: Record<string, string> = { en: 'en-IN', hi: 'hi-IN', kn: 'kn-IN', hinglish: 'hinglish' }
const TILES = [
  ['attempts', 'Attempts', 'ink'], ['blocked_claims', 'Blocked claims', 'stamp'], ['refused_actions', 'Refused actions', 'stamp'],
  ['escalations', 'Handed to a person', 'warn'], ['forbidden_executed', 'Forbidden executed', 'ok'],
] as const

export function Judge({ personaArg }: { personaArg: string | null }) {
  const { api } = useLive()
  const m = useMotion()
  const [personas, setPersonas] = useState<PersonaCard[]>([])
  const [cards, setCards] = useState<BreakItCard[]>([])
  const [steps, setSteps] = useState<TourStep[]>([])
  const [view, setView] = useState<JudgeView | null>(null)
  const [tour, setTour] = useState<TourProgress | null>(null)
  const [twinOn, setTwinOn] = useState(false)
  const [why, setWhy] = useState<number | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const fail = (e: unknown) => setErr(e instanceof Error ? e.message : String(e))
  const conv = useConversation({ api, view, onView: setView })

  useEffect(() => {
    api.personas().then((r) => setPersonas(r.personas)).catch(fail)
    api.breakIt().then((r) => setCards(r.cards)).catch(fail)
    api.tourSteps().then((r) => setSteps(r.steps)).catch(fail)
  }, [api])

  const refreshTour = (sid: string) => api.tourProgress(sid).then(setTour).catch(fail)
  const start = (personaId: string) => api.judgeStart(personaId).then((v) => {
    setView(v); conv.reset(); setErr(null); setTwinOn(false); conv.setBanner(null); void refreshTour(v.session.session_id)
  }).catch(fail)
  useEffect(() => { if (personaArg && !view) void start(personaArg) }, [personaArg])      // eslint-disable-line react-hooks/exhaustive-deps

  const send = async (text: string, language?: string) => {
    await conv.say(text, { twin: twinOn, language })
    if (view) void refreshTour(view.session.session_id)
  }
  const toggleTwin = async () => {
    const on = !twinOn
    setTwinOn(on)
    if (on) { const s = await api.twinStart().catch((e) => { fail(e); return null }); conv.setBanner(s?.banner ?? null) } else conv.setBanner(null)
  }

  const sid = view?.session.session_id
  return (
    <div className="mx-auto max-w-7xl px-6 py-10">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-label text-muted">Judge Mode</div>
          <h1 className="text-h1 mt-1">{view ? 'Try to break it.' : 'Pick a merchant to talk to.'}</h1>
        </div>
        {view && <Button onClick={() => { setView(null); conv.reset(); setTwinOn(false); conv.setBanner(null); setTour(null) }}>Change merchant</Button>}
      </div>
      {err && <p role="alert" className="text-bad mt-4">{err}</p>}

      {!view && (
        <>
          <p className="text-body text-muted mt-4 max-w-[60ch]">Every merchant here is synthetic. What you type goes through the same path as a real message: understood, checked by policy, acted on through one executor, and answered only with sentences that can be proven.</p>
          <ul className="mt-10 grid gap-px border border-line bg-line sm:grid-cols-2 lg:grid-cols-3">
            {personas.map((p, i) => (
              <motion.li key={p.persona_id} className="bg-bg" initial={{ opacity: 0, y: m.y(14) }} animate={{ opacity: 1, y: 0 }} transition={{ duration: m.d(0.5), delay: m.reduce ? 0 : i * STAGGER, ease: ease.out }}>
                <button type="button" onClick={() => void start(p.persona_id)} className="group flex h-full w-full flex-col items-start gap-3 p-6 text-left transition-colors duration-150 hover:bg-surface">
                  <span className="text-label text-muted">{p.persona_id} · {p.merchant_id}</span>
                  <span className="text-h3">{p.title}</span>
                  <span className="text-small text-muted">{p.description}</span>
                  <span className="text-label mt-auto text-stamp opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-visible:opacity-100">Talk as this merchant →</span>
                </button>
              </motion.li>
            ))}
          </ul>
        </>
      )}

      {view && (
        <>
          {conv.banner && (
            <div role="alert" data-testid="twin-banner" className="mt-6 border-2 border-bad px-4 py-3 text-center text-label text-bad">{conv.banner}</div>
          )}
          <div className="mt-8 grid gap-8 lg:grid-cols-[1fr_24rem]">
            <section aria-label="Conversation">
              <div className={`grid gap-4 ${twinOn ? 'md:grid-cols-2' : ''}`}>
                <SurfaceBox surface="paper" className="ruled border border-line p-6">
                  <div className="mb-4 flex items-center justify-between">
                    <span className="text-label text-muted">{twinOn ? 'Real Munim' : 'The Counter'} · {view.session.merchant_id}</span>
                    {twinOn && <Stamp tone="ok">guards on</Stamp>}
                  </div>
                  <ChatLog turns={conv.turns} onWhy={setWhy} empty={<p className="text-muted">Say something as {view.session.merchant_id}, or take a card from the break-it deck.</p>} />
                </SurfaceBox>
                {twinOn && (
                  <div className="border-2 border-bad bg-bg p-6">
                    <div className="mb-4 flex items-center justify-between">
                      <span className="text-label text-bad">Guard-off twin · simulated</span>
                      <Stamp tone="bad">guards off</Stamp>
                    </div>
                    <ChatLog turns={conv.twinTurns} onWhy={setWhy} empty={<p className="text-muted">The twin answers the same message with every guard removed.</p>} />
                    {conv.twinActions.length > 0 && (
                      <p data-testid="twin-actions" className="rule-t text-small mt-5 pt-3 text-bad">The twin did: {conv.twinActions.join(', ')}</p>
                    )}
                  </div>
                )}
              </div>
              <Composer onSend={(t) => void send(t)} busy={conv.busy}>
                <Button aria-pressed={twinOn} onClick={() => void toggleTwin()}>{twinOn ? 'Hide the twin' : 'Show the guard-off twin'}</Button>
                <Button onClick={() => sid && api.judgeFine(sid).then((r) => setView({ ...view, scoreboard: r.scoreboard })).catch(fail)}>That was fine</Button>
              </Composer>
              {conv.error && <p role="alert" className="text-bad mt-3">{conv.error}</p>}
            </section>

            <aside className="space-y-10">
              <Reveal>
                <section aria-label="Scoreboard">
                  <div className="flex items-center justify-between"><h2 className="text-h3">Scoreboard</h2><Tag>counted from the ledger</Tag></div>
                  <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-6">
                    {TILES.map(([k, label, tone]) => <Readout key={k} label={label} value={view.scoreboard[k]} tone={tone} />)}
                    <Readout label="“That was fine” marks" value={view.scoreboard.fine_marks} tone="muted" />
                  </dl>
                  <p className="text-small text-muted mt-3">Simulated data. Forbidden executed must stay at 0.</p>
                </section>
              </Reveal>

              <section aria-label="Break-it deck">
                <h2 className="text-h3">Try to break it</h2>
                <ul className="mt-3 space-y-3">
                  {cards.map((c) => (
                    <li key={c.id} className="border border-line p-3">
                      <div className="font-semibold">{c.title}</div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        {c.tries.map((t, i) => (
                          <Button key={i} className="min-h-9 px-3 text-small" disabled={conv.busy} onClick={() => void send(t.text, LANG[t.language])} title={t.text}>{t.language}</Button>
                        ))}
                      </div>
                    </li>
                  ))}
                </ul>
              </section>

              <section aria-label="Guided tour">
                <div className="flex items-center justify-between"><h2 className="text-h3">Tour</h2><span className="text-num text-muted">{tour ? `${tour.done} / ${tour.total}` : ''}</span></div>
                <ol className="mt-3 space-y-2">
                  {steps.map((s) => {
                    const done = tour?.steps.find((x) => x.id === s.id)?.done
                    const next = tour?.next === s.id
                    return (
                      <li key={s.id} data-done={done ? 'yes' : 'no'} className={done ? 'text-muted' : ''}>
                        <span aria-hidden="true" className={`mr-2 inline-block w-4 ${done ? 'text-ok' : 'text-muted'}`}>{done ? '✓' : '·'}</span>
                        <span className={next ? 'font-semibold text-fg' : ''}>{s.title}</span>
                        {next && <span className="text-small block pl-6 text-muted">{s.instruction}</span>}
                      </li>
                    )
                  })}
                </ol>
              </section>

              <a className="inline-flex min-h-11 items-center rounded-[4px] border border-line px-5 font-semibold no-underline hover:border-fg" href={`/api/export/judge/${encodeURIComponent(view.session.session_id)}?format=html`}>Download the evidence pack</a>
            </aside>
          </div>
        </>
      )}
      <WhyDrawer api={api} seq={why} onClose={() => setWhy(null)} />
    </div>
  )
}
