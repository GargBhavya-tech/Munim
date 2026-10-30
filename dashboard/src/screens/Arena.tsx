import { useCallback, useEffect, useState } from 'react'
import { useLive } from '../hooks/live'
import { btnPrimary, ErrorNote, Panel } from '../components/common'
import type { ArenaInfo, ArenaPlayer } from '../api/types'

export function Arena() {
  const { api } = useLive()
  const [info, setInfo] = useState<ArenaInfo | null>(null)
  const [nick, setNick] = useState('')
  const [player, setPlayer] = useState<ArenaPlayer | null>(null)
  const [text, setText] = useState('')
  const [log, setLog] = useState<string[]>([])
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
      setLog((l) => [...l, `You: ${t}`, ...(r.replies ?? []).map((x) => `Munim: ${x.text}`), ...((r.cleared ?? []).length ? [`LEVEL CLEARED: ${(r.cleared ?? []).join(', ')}`] : [])])
      setErr(null)
      load()
    }).catch(fail)
  }
  const spent = info ? Math.round((info.budget.spent_paise / info.budget.budget_paise) * 100) : 0
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-bold">Break Munim</h1>
      <p className="text-sm">Try to make Munim do something it must never do. Every level is checked against the ledger, not against what Munim said. Everything here is SIMULATED.</p>
      <ErrorNote error={err} />
      {info && info.state !== 'RUNNING' && <p role="alert" data-testid="arena-stopped" className="rounded bg-amber-100 p-2 font-bold">The arena is {info.state.toLowerCase()}.</p>}
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title={player ? `Playing as ${player.nickname}` : 'Join'}>
          {!player ? (
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void join() }}>
              <input aria-label="Nickname" className="flex-1 rounded border border-slate-400 px-2 py-1" value={nick} onChange={(e) => setNick(e.target.value)} placeholder="3-16 letters or digits" />
              <button type="submit" className={btnPrimary}>Join</button>
            </form>
          ) : (
            <>
              <div data-testid="arena-log" className="mb-2 flex min-h-32 flex-col gap-1">{log.map((l, i) => <p key={i} className={l.startsWith('LEVEL') ? 'font-bold text-red-700' : ''}>{l}</p>)}</div>
              <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); attempt() }}>
                <input aria-label="Your attack" className="flex-1 rounded border border-slate-400 px-2 py-1" value={text} onChange={(e) => setText(e.target.value)} />
                <button type="submit" className={btnPrimary}>Try it</button>
              </form>
            </>
          )}
          {info && <ul data-testid="arena-levels" className="mt-3 list-disc pl-5 text-sm">{info.levels.map((l) => <li key={l.id}>{l.title}</li>)}</ul>}
          {info && <p className="mt-2 text-xs text-slate-600">Arena budget used: {spent}%</p>}
        </Panel>
        <Panel title="Leaderboard">
          <table className="w-full text-left">
            <thead><tr className="border-b border-slate-400"><th>#</th><th>Player</th><th>Breaks</th><th>Attacks stopped</th><th>Attempts</th></tr></thead>
            <tbody data-testid="leaderboard">{(info?.leaderboard ?? []).map((r) => <tr key={r.rank}><td>{r.rank}</td><td>{r.nickname}</td><td>{r.breaks}</td><td>{r.blocked}</td><td>{r.attempts}</td></tr>)}</tbody>
          </table>
        </Panel>
      </div>
    </div>
  )
}
