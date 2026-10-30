import { useEffect, useState } from 'react'
import { arenaApi, type AttemptResult, type Info } from './api'

export function ArenaPage() {
  const [info, setInfo] = useState<Info | null>(null)
  const [nick, setNick] = useState('')
  const [token, setToken] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [last, setLast] = useState<AttemptResult | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const refresh = () => { arenaApi.info().then(setInfo).catch(() => setMsg('Could not reach the arena.')) }
  useEffect(refresh, [])

  const join = async () => {
    const r = await arenaApi.join(nick)
    if (r.ok) { setToken(r.token); setMsg(null); refresh() } else setMsg(r.reason || r.error)
  }
  const send = async () => {
    if (!token) return
    const r = await arenaApi.attempt(token, text)
    if (r.ok) { setLast(r.data); setMsg(null); setText(''); refresh() } else setMsg(r.reason || r.error)
  }
  const stopped = info && info.state !== 'RUNNING'
  const levels = last?.levels ?? info?.levels ?? []
  return (
    <main className="mx-auto max-w-xl space-y-4 p-4" data-testid="arena">
      <h1 className="text-xl font-bold">Break Munim</h1>
      <p className="text-xs text-slate-500">SIMULATED. Nothing here is real money or real data. Try to make Munim do something it must never do.</p>
      {stopped && <p data-testid="arena-stopped" role="alert" className="rounded bg-amber-100 p-2 text-sm">The arena is closed ({info!.state === 'KILLED' ? 'switched off' : 'budget used up'}).</p>}
      {!token ? (
        <div className="flex gap-2">
          <input aria-label="Nickname" className="flex-1 rounded border px-2 py-1" value={nick} maxLength={16} onChange={(e) => setNick(e.target.value)} />
          <button className="rounded bg-slate-800 px-3 py-1 text-white" onClick={join} disabled={!!stopped}>Play</button>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex gap-2">
            <input aria-label="Attack" className="flex-1 rounded border px-2 py-1" value={text} onChange={(e) => setText(e.target.value)} />
            <button className="rounded bg-slate-800 px-3 py-1 text-white" onClick={send} disabled={!!stopped || !text.trim()}>Try it</button>
          </div>
          {last && last.replies.map((r, i) => <p key={i} className="rounded bg-slate-100 p-2 text-sm">{r.text}</p>)}
          {last && last.replies.length === 0 && <p className="text-sm italic text-slate-500">Munim said nothing.</p>}
        </div>
      )}
      {msg && <p data-testid="arena-msg" className="text-sm text-red-700">{msg}</p>}
      <ol className="space-y-1" data-testid="levels">
        {levels.map((l) => (
          <li key={l.id} data-testid={`level-${l.id}`} data-broken={l.broken ? 'true' : 'false'} className="rounded border p-2 text-sm">
            <b>{l.order}. {l.title}</b> {l.broken ? 'BROKEN' : ''}<br /><span className="text-slate-600">{l.goal}</span>
          </li>
        ))}
      </ol>
      <h2 className="font-semibold">Leaderboard</h2>
      <table className="w-full text-sm" data-testid="leaderboard">
        <thead><tr><th align="left">#</th><th align="left">Player</th><th>Breaks</th><th>Stopped</th><th>Tries</th></tr></thead>
        <tbody>{(info?.leaderboard ?? []).map((r) => (
          <tr key={r.rank}><td>{r.rank}</td><td>{r.nickname}</td><td align="center">{r.breaks}</td><td align="center">{r.blocked}</td><td align="center">{r.attempts}</td></tr>
        ))}</tbody>
      </table>
    </main>
  )
}
