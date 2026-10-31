import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import type { Channel, Lead } from '../leads'

/** A real street map (OpenStreetMap tiles, darkened, Leaflet) with one marker per lead. The tiles are only the backdrop: every lead's position, size and channel still come from
 *  the server. The positions themselves are synthetic, so the markers sit on real streets but are not real shops. */

const COLOR: Record<Channel, string> = { field_rep: '#ff5a36', ai_voice: '#7aa2ff', self_serve: '#9aa0aa' }
const TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'                                                  // keyless; darkened below so it sits in the dark UI
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

function icon(l: Lead, top: number, picked: boolean): L.DivIcon {
  const size = Math.round(12 + 14 * ((l.score ?? 0) / top))
  const c = COLOR[l.channel as Channel]
  const shape = l.channel === 'ai_voice' ? 'border-radius:50%' : l.channel === 'self_serve' ? 'transform:rotate(45deg)' : 'border-radius:2px'
  const ring = picked ? 'box-shadow:0 0 0 3px #fff,0 0 0 5px rgba(255,255,255,.35)' : 'box-shadow:0 0 0 1.5px rgba(10,12,16,.85)'
  return L.divIcon({ className: '', iconSize: [size, size], iconAnchor: [size / 2, size / 2], html: `<div style="width:${size}px;height:${size}px;background:${c};${shape};${ring};opacity:.92"></div>` })
}

export function GeoMap({ leads, picked, onPick, label }: { leads: Lead[]; picked: string | null; onPick: (id: string) => void; label: string }) {
  const box = useRef<HTMLDivElement>(null)
  const map = useRef<L.Map | null>(null)
  const layer = useRef<L.LayerGroup | null>(null)
  const fitted = useRef(new WeakMap<L.Map, string>())                                                           // what each map instance was last fitted to

  useEffect(() => {
    if (!box.current) return
    const m = L.map(box.current, { zoomControl: true, scrollWheelZoom: false, attributionControl: true })
    m.setView([15.4, 75.0], 7)                                                                                    // the map needs a view before a layer is added, or the layer has no container yet
    const tiles = L.tileLayer(TILES, { attribution: ATTRIBUTION, maxZoom: 19 }).addTo(m)
    tiles.getContainer()?.style.setProperty('filter', 'invert(1) hue-rotate(190deg) brightness(.85) contrast(.9) saturate(.55)')          // markers live in a different pane, so they keep their colours
    layer.current = L.layerGroup().addTo(m)
    map.current = m
    return () => { m.remove(); map.current = null; layer.current = null }
  }, [])

  useEffect(() => {
    const m = map.current
    const g = layer.current
    if (!m || !g) return
    g.clearLayers()
    const top = Math.max(1, ...leads.map((l) => l.score ?? 0))
    const pts: L.LatLngTuple[] = []
    for (const l of leads) {
      if (l.lat === null || l.lng === null || !l.channel) continue
      pts.push([l.lat, l.lng])
      const mk = L.marker([l.lat, l.lng], { icon: icon(l, top, l.lead_id === picked), keyboard: true, title: l.name, zIndexOffset: l.lead_id === picked ? 1000 : Math.round(l.score ?? 0) })
      mk.bindTooltip(`${l.name}${l.score !== null ? ` · priority ${Math.round(l.score)}` : ''}`, { direction: 'top', offset: [0, -6] })
      mk.on('click', () => onPick(l.lead_id))
      mk.addTo(g)
    }
    const key = `${label}:${pts.length}`
    if (pts.length && fitted.current.get(m) !== key) {                                                                    // refit when the city or the set changes, not on every click
      fitted.current.set(m, key)
      m.invalidateSize()
      m.fitBounds(L.latLngBounds(pts), { padding: [28, 28], maxZoom: 15, animate: false })
    }
  }, [leads, picked, onPick, label])

  useEffect(() => {                                                                                                // bring the picked lead into view
    const m = map.current
    const l = leads.find((x) => x.lead_id === picked)
    if (m && l && l.lat !== null && l.lng !== null && !m.getBounds().contains([l.lat, l.lng])) m.panTo([l.lat, l.lng])
  }, [picked, leads])

  return <div ref={box} role="application" aria-label={`Street map of ${leads.length} leads in ${label}. The ranked list has the same leads.`} className="h-[34rem] w-full border border-line" />
}
