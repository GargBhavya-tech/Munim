/** The leads API (core/api/routes_leads.py) as the Leads screen uses it. Every value comes from the server: the screen reports what the discovery, duplicate matching
 *  and scoring jobs decided, and what the brief job wrote. Nothing here ranks, scores or estimates anything. */

export type Channel = 'field_rep' | 'ai_voice' | 'self_serve'

export interface Lead {
  lead_id: string
  name: string
  category: string | null
  pincode: string | null
  lat: number | null
  lng: number | null
  source_id: string | null
  confidence: number | null
  gst_slab: string | null
  state: string
  score: number | null
  channel: Channel | null
  uncertain: boolean
  monthly_rupees: number | null
  low_rupees: number | null
  high_rupees: number | null
  do_not_contact: boolean
}

export interface Funnel {
  found: number
  existing_merchant: number
  needs_review: number
  duplicate: number
  cleared: number
  not_checked: number
}

export interface Bounds { min_lat: number; max_lat: number; min_lng: number; max_lng: number }

export interface Excluded {
  lead_id: string
  name: string
  state: string
  category: string | null
  pincode: string | null
  matched_merchant_id: string | null
  duplicate_of: string | null
  reason: string
}

export interface LeadsView { funnel: Funnel; bounds: Bounds | null; leads: Lead[]; excluded: Excluded[] }

export interface SourcedLine { id: string; text: string; source: string }
export interface Brief {
  passes_gate: boolean
  who: SourcedLine[]
  uses_now: string[]
  needs: string[]
  pitch: (SourcedLine & { rank: string; label: string })[]
  objections: { key: string; source: string; text: string }[]
  contact: SourcedLine[]
  contact_notes: string[]
  gaps: string[]
}

export interface Contact {
  segment: string
  order: string[]
  consented_channels: string[]
  chosen_channel: string | null
  do_not_contact: boolean
  can_contact_now: boolean
  why_not: string | null
}

export interface LeadDetail {
  lead: Lead
  provenance: { fields: Record<string, { source: string; confidence: number; fetched_at: string }>; dedupe: Record<string, unknown> }
  contact: Contact
  brief: Brief
}

export type LeadResult = { ok: true; detail: LeadDetail } | { ok: false; reason: string; matchedMerchant: string | null }

/** The server's own words for a failure: FastAPI errors here are {detail: {error, reason}} or {detail: "CODE"}. */
async function fail(r: Response): Promise<never> {
  let msg = `HTTP ${r.status}`
  try {
    const b = await r.json()
    const d = b?.detail
    msg = typeof d === 'string' ? d : d?.reason ? `${d.error}: ${d.reason}` : d?.error ?? msg
  } catch { /* not JSON: the status is the message */ }
  throw new Error(msg)
}

export async function getLeads(): Promise<LeadsView> {
  const r = await fetch('/api/leads')
  return r.ok ? r.json() : fail(r)
}

/** A lead that may not be briefed (an existing merchant, say) is an answer, not an error: the server says why. */
export async function getLead(id: string): Promise<LeadResult> {
  const r = await fetch(`/api/leads/${encodeURIComponent(id)}`)
  if (r.ok) return { ok: true, detail: await r.json() }
  if (r.status === 409) {
    const d = (await r.json().catch(() => null))?.detail
    return { ok: false, reason: d?.reason ?? 'This lead cannot be briefed.', matchedMerchant: d?.matched_merchant_id ?? null }
  }
  return fail(r)
}

export const CHANNEL_LABEL: Record<string, string> = {
  field_rep: 'Field visit',
  ai_voice: 'AI voice or WhatsApp',
  self_serve: 'Self-serve link',
  field_visit: 'Field visit',
  whatsapp: 'WhatsApp',
  voice: 'Voice call',
  email: 'Email',
}

export const STATE_LABEL: Record<string, string> = {
  EXISTING_MERCHANT: 'Already a Paytm merchant',
  DUPLICATE: 'Duplicate of a better lead',
  NEEDS_REVIEW: 'Possible match, a person decides',
}

export const inr = (n: number | null): string => (n === null ? 'unknown' : `₹${n.toLocaleString('en-IN')}`)
export const nice = (s: string | null): string => (s ?? 'unknown').replace(/_/g, ' ')

/** Where a lead sits on the map. Longitude is scaled by cos(latitude) so the ward keeps its real shape; the result is in a w by h box, north up. */
export function frame(b: Bounds, width: number): { w: number; h: number; at: (lat: number, lng: number) => [number, number] } {
  const mid = ((b.min_lat + b.max_lat) / 2) * (Math.PI / 180)
  const dx = Math.max((b.max_lng - b.min_lng) * Math.cos(mid), 1e-9)
  const dy = Math.max(b.max_lat - b.min_lat, 1e-9)
  const h = Math.min(Math.max(width * (dy / dx), width * 0.45), width * 0.9)
  return { w: width, h, at: (lat, lng) => [((lng - b.min_lng) / (b.max_lng - b.min_lng || 1)) * width, ((b.max_lat - lat) / (b.max_lat - b.min_lat || 1)) * h] }
}
