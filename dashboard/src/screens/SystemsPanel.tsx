import { useState, type ReactNode } from 'react'
import { useFlash } from '../hooks/useFlash'
import { useLive, useResource } from '../hooks/live'
import { causesByTile, type TileCause, type TileId } from '../lib/systems'
import { ErrorNote, Json, Loading, SimulatedBadge } from '../components/common'

export function Tile({ id, title, data, cause, children }: { id: TileId; title: string; data: unknown; cause?: TileCause; children?: ReactNode }) {
  const flash = useFlash(data)
  return (
    <section
      data-testid={`tile-${id}`}
      data-flash={flash ? 'on' : 'off'}
      aria-label={title}
      className={`rounded-lg border-2 p-3 transition-colors ${flash ? 'border-amber-500 bg-amber-100' : 'border-slate-300 bg-white'}`}
    >
      <h2 className="mb-1 text-lg font-semibold">{title}</h2>
      <p aria-live="polite" className={flash ? 'text-sm font-bold text-amber-900' : 'sr-only'}>
        {flash ? `${title} updated` : ''}
      </p>
      {children ?? <Json value={data} />}
      {cause && (
        <p className="mt-2 text-sm text-slate-700" data-testid={`cause-${id}`}>
          last change: <b>{cause.action}</b>{cause.key && <> | key <code>{cause.key}</code></>}
        </p>
      )}
    </section>
  )
}

export function SystemsPanel() {
  const { api, recent } = useLive()
  const merchants = useResource(() => api.cases(), [api])
  const ids = [...new Set((merchants.data?.cases ?? []).map((c) => c.merchant_id))].sort()
  const [chosen, setChosen] = useState<string | null>(null)
  const mid = chosen ?? ids[0] ?? null
  // Always read from the server; a ledger event triggers a fresh read, tiles never hold their own cache.
  const { data, error } = useResource(() => (mid ? api.systems(mid) : Promise.reject(new Error('no merchant'))), [api, mid], () => true)
  const causes = causesByTile(recent)
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">Systems</h1>
        <SimulatedBadge />
        <label className="flex items-center gap-2">
          Merchant
          <select className="rounded border border-slate-400 px-2 py-1" value={mid ?? ''} onChange={(e) => setChosen(e.target.value)}>
            {ids.map((i) => <option key={i}>{i}</option>)}
          </select>
        </label>
      </div>
      {mid === null ? <p>No merchants yet.</p> : error && !data ? <ErrorNote error={error} /> : !data ? <Loading /> : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          <Tile id="kyc" title="KYC" data={data.kyc} cause={causes.kyc} />
          <Tile id="bank" title="Bank" data={data.bank} cause={causes.bank} />
          <Tile id="device" title="Device" data={data.device} cause={causes.device} />
          <Tile id="payments" title="Payments and settlement" data={data.payments} cause={causes.payments} />
          <Tile id="crm" title="CRM" data={data.crm} cause={causes.crm}>
            <p>Open cases: <b>{data.crm.open_cases.length}</b></p>
            <p>Focus: <b>{data.crm.focus ?? 'none'}</b></p>
            <p>Language: <b>{data.crm.language}</b></p>
          </Tile>
          <Tile id="pricing" title="Pricing" data={data.pricing} cause={causes.pricing}>
            <Json value={data.pricing.band} />
            <p>Waivers: <b>{data.pricing.waivers.length}</b></p>
            {data.pricing.waivers.length > 0 && <Json value={data.pricing.waivers} />}
          </Tile>
          <Tile id="scheduler" title="Scheduler" data={data.scheduler} cause={causes.scheduler} />
          <Tile id="tickets" title="Ticketing" data={data.tickets} cause={causes.tickets} />
          <Tile id="handoff" title="Handoff queue" data={data.handoff_queue} cause={causes.handoff}>
            <p className="text-3xl font-bold">{data.handoff_queue.open} open</p>
          </Tile>
          <Tile id="comms" title="Comms outbox (last 10)" data={data.comms} cause={causes.comms} />
        </div>
      )}
    </div>
  )
}
