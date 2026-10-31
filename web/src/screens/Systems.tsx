import { useState, type ReactNode } from 'react'
import { useLive, useResource } from '@old/hooks/live'
import { useFlash } from '@old/hooks/useFlash'
import { causesByTile, type TileCause, type TileId } from '@old/lib/systems'
import { Stamp } from '../ui'
import { fieldSm, Json, loading, Note, Page } from '../parts'

/** The ten mock Paytm systems for one merchant, read fresh from the server on every ledger event. A tile that just changed lights up and says which action caused it. */

function Tile({ id, title, data, cause, children }: { id: TileId; title: string; data: unknown; cause?: TileCause; children?: ReactNode }) {
  const flash = useFlash(data)
  return (
    <section data-tile={id} aria-label={title} data-flash={flash ? 'on' : 'off'} className={`border p-4 transition-colors duration-500 ${flash ? 'border-stamp bg-surface' : 'border-line'}`}>
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-label text-fg">{title}</h2>
        {flash && <Stamp>updated</Stamp>}
      </div>
      <div className="mt-3">{children ?? <Json value={data} />}</div>
      {cause && <p className="text-small text-muted mt-3">last change: <b className="text-fg">{cause.action}</b>{cause.key && <> · key <code className="text-num">{cause.key.slice(0, 12)}</code></>}</p>}
    </section>
  )
}

export function Systems() {
  const { api, recent } = useLive()
  const merchants = useResource(() => api.cases(), [api])
  const ids = [...new Set((merchants.data?.cases ?? []).map((c) => c.merchant_id))].sort()
  const [chosen, setChosen] = useState<string | null>(null)
  const mid = chosen ?? ids[0] ?? null
  const { data, error } = useResource(() => (mid ? api.systems(mid) : Promise.reject(new Error('no merchant'))), [api, mid], () => true)
  const causes = causesByTile(recent)
  return (
    <Page eyebrow="Systems" title="What each system holds, right now." intro="These are the mock Paytm systems Munim acts on. A change shows up here as it happens, with the action that caused it.">
      <label className="text-small inline-flex items-center gap-2 text-muted">Merchant
        <select className={fieldSm} value={mid ?? ''} onChange={(e) => setChosen(e.target.value)}>{ids.map((i) => <option key={i}>{i}</option>)}</select>
      </label>
      <div className="mt-6">
        {mid === null ? <p className="text-muted">No merchants yet. Open Judge Mode and send a message.</p> : error && !data ? <Note error={error} /> : !data ? loading : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            <Tile id="kyc" title="KYC" data={data.kyc} cause={causes.kyc} />
            <Tile id="bank" title="Bank" data={data.bank} cause={causes.bank} />
            <Tile id="device" title="Device" data={data.device} cause={causes.device} />
            <Tile id="payments" title="Payments and settlement" data={data.payments} cause={causes.payments} />
            <Tile id="crm" title="CRM" data={data.crm} cause={causes.crm}>
              <p>Open cases: <b className="text-num">{data.crm.open_cases.length}</b></p>
              <p>Focus: <b>{data.crm.focus ?? 'none'}</b></p>
              <p>Language: <b>{data.crm.language}</b></p>
            </Tile>
            <Tile id="pricing" title="Pricing" data={data.pricing} cause={causes.pricing}>
              <Json value={data.pricing.band} />
              <p className="mt-2">Waivers: <b className="text-num">{data.pricing.waivers.length}</b></p>
              {data.pricing.waivers.length > 0 && <Json value={data.pricing.waivers} />}
            </Tile>
            <Tile id="scheduler" title="Scheduler" data={data.scheduler} cause={causes.scheduler} />
            <Tile id="tickets" title="Ticketing" data={data.tickets} cause={causes.tickets} />
            <Tile id="handoff" title="Handoff queue" data={data.handoff_queue} cause={causes.handoff}>
              <p className="text-display text-num">{data.handoff_queue.open}</p><p className="text-small text-muted">open</p>
            </Tile>
            <Tile id="comms" title="Comms outbox (last 10)" data={data.comms} cause={causes.comms} />
          </div>
        )}
      </div>
    </Page>
  )
}
