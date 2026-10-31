import { useCallback, useEffect, useState } from 'react'
import { useLive } from '@old/hooks/live'
import type { ArenaInfo, ArenaPlayer } from '@old/api/types'
import { Button, Stamp } from '../ui'
import { field, Note, Page, Section } from '../parts'

/** Break Munim: try to make it do something it must never do. Every level is judged from the ledger, never from what Munim said. */

type Line = { who: 'you' | 'munim' | 'cleared'; text: string }

export function Arena() {
  const { api } = useLive()
  const [info, setInfo] = useState<ArenaInfo | null>(null)
  const [nick, setNick] = useState('')
  const [player, setPlayer] = useState<ArenaPlayer | null>(null)
  const [text, setText] = useState('')
  const [log, setLog] = useState<Line[]>([])
  const [err, setErr] = useState<string | null>(null)
  const fail = (e: unknown) => setErr(e instanceof Error ? e.message : String(e))
  const load = useCallback(() => { api.arenaInfo().then(setInfo).catch(fail) }, [api])
  useEffect(load, [load])

  const join = () => api.arenaJoin(nick.trim()).then((p) => { setPlayer(p); setErr(null) }).catch(fail)
  const attempt = () => {
    if (!player || !text.trim()) return
    const t = text.trim()
    setText('')
    api.arenaAttempt(player.token, t).then((r) => {
      setLog((l) => [...l, { who: 'you', text: t }, ...(r.replies ?? []).map((x) => ({ who: 'munim' as const, text: x.text })), ...((r.cleared ?? []).length ? [{ who: 'cleared' as const, text: `Level cleared: ${(r.cleared ?? []).join(', ')}` }] : [])])
      setErr(null)
      load()
    }).catch(fail)
  }
  const spent = info ? Math.round((info.budget.spent_paise / info.budget.budget_paise) * 100) : 0
  return (
    <Page eyebrow="Arena" title="Break Munim." intro="Try to make Munim do something it must never do. A level is cleared only if the ledger shows it happened. Everything here is simulated.">
      <Note error={err} />
      {info && info.state !== 'RUNNING' && <p role="alert" className="mb-6"><Stamp tone="warn">the arena is {info.state.toLowerCase()}</Stamp></p>}
      <div className="grid gap-12 lg:grid-cols-2">
        <Section title={player ? `Playing as ${player.nickname}` : 'Join'}>
          {!player ? (
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void join() }}>
              <input aria-label="Nickname" className={`${field} flex-1`} value={nick} onChange={(e) => setNick(e.target.value)} placeholder="3-16 letters or digits" />
              <Button type="submit" variant="primary">Join</Button>
            </form>
          ) : (
            <>
              <div aria-live="polite" className="ruled mb-3 min-h-32 space-y-2 py-2">
                {log.length === 0 && <p className="text-muted">Say your first attack.</p>}
                {log.map((l, i) => l.who === 'cleared'
                  ? <p key={i}><Stamp>{l.text}</Stamp></p>
                  : <p key={i} className={l.who === 'munim' ? 'text-voice' : 'text-right text-muted'}>{l.text}</p>)}
              </div>
              <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); attempt() }}>
                <input aria-label="Your attack" className={`${field} flex-1`} value={text} onChange={(e) => setText(e.target.value)} />
                <Button type="submit" variant="primary" disabled={!text.trim()}>Try it</Button>
              </form>
            </>
          )}
          {info && (
            <>
              <h3 className="text-label mt-8 text-muted">Levels</h3>
              <ul className="mt-2 list-disc pl-5">{info.levels.map((l) => <li key={l.id}>{l.title}</li>)}</ul>
              <p className="text-small mt-4 text-muted">Arena budget used: <span className="text-num">{spent}%</span></p>
            </>
          )}
        </Section>
        <Section title="Leaderboard">
          <table className="w-full text-left">
            <thead><tr className="rule-b text-label text-muted"><th className="py-2 font-normal">#</th><th className="font-normal">Player</th><th className="font-normal">Breaks</th><th className="font-normal">Attacks stopped</th><th className="font-normal">Attempts</th></tr></thead>
            <tbody>{(info?.leaderboard ?? []).map((r) => <tr key={r.rank} className="rule-b"><td className="text-num py-2">{r.rank}</td><td>{r.nickname}</td><td className="text-num">{r.breaks}</td><td className="text-num">{r.blocked}</td><td className="text-num">{r.attempts}</td></tr>)}</tbody>
          </table>
          {info && info.leaderboard.length === 0 && <p className="mt-3 text-muted">No one has played yet.</p>}
        </Section>
      </div>
    </Page>
  )
}
