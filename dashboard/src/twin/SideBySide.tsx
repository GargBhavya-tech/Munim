import { useState } from 'react'
import { twinApi } from './api'
import { TwinBanner } from './TwinBanner'
import type { FanOut, Reply } from './types'

function Col({ title, replies, children }: { title: string; replies: Reply[]; children?: React.ReactNode }) {
  return (
    <section className="flex-1 space-y-2 rounded border border-slate-300 p-3">
      <h3 className="font-semibold">{title}</h3>
      {children}
      {replies.length === 0 ? <p data-testid="no-reply" className="text-sm italic text-slate-500">Said nothing.</p> :
        replies.map((r, i) => <p key={i} className="rounded bg-slate-100 p-2 text-sm">{r.text}</p>)}
    </section>
  )
}

export function SideBySide({ merchantId }: { merchantId: string }) {
  const [text, setText] = useState('')
  const [out, setOut] = useState<FanOut | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const send = async () => {
    const r = await twinApi.send(merchantId, text)
    if (r.ok) { setOut(r.data); setErr(null) } else setErr(r.error)
  }
  return (
    <div className="space-y-3" data-testid="side-by-side">
      <p className="text-xs text-slate-500">SIMULATED. One message goes to both engines. The right one runs on a private copy of the data and sends nothing real.</p>
      <div className="flex gap-2">
        <input aria-label="Message" className="flex-1 rounded border px-2 py-1" value={text} onChange={(e) => setText(e.target.value)} />
        <button className="rounded bg-slate-800 px-3 py-1 text-white" onClick={send} disabled={!text.trim()}>Send to both</button>
      </div>
      {err && <p data-testid="twin-error" className="text-sm text-red-700">{err}</p>}
      <div className="flex flex-col gap-3 md:flex-row">
        <Col title="Munim (guards on)" replies={out?.real.replies ?? []} />
        <div className="flex-1 space-y-2">
          <TwinBanner />
          <Col title="Twin (guards off)" replies={out?.twin.replies ?? []}>
            {out && out.twin.actions.filter((a) => a.cls !== 'R0').map((a, i) => <p key={i} className="text-xs">ran: {a.action} ({a.status})</p>)}
            {out && out.twin.nonexistent_actions_requested.map((a, i) => <p key={i} className="text-xs">asked for {a}: does not exist, nothing ran</p>)}
          </Col>
        </div>
      </div>
    </div>
  )
}
