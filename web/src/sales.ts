/** The sales API (core/api/routes_sales.py) as the Sales screen uses it. Every number, ranking and plan is the server's: nothing here computes a value, a chance or an approach. */
import { call } from './ops'

export interface DipFinding {
  merchant_id: string
  name: string | null
  diagnosis: string
  action: string
  what: string
  why: string
  detail: { ratios?: number[]; area_median_ratio?: number | null; persistent_days?: number; zero_days?: number; failed_settlements?: number; pincode?: string; offline_share?: number }
  quiet: boolean
  retention: boolean
}
export interface DipsView { as_of: string; findings: DipFinding[]; checked: number; rules: { persist_days: number; drop_ratio: number; zero_days: number; actions: Record<string, string> } }

export interface WorkItem {
  kind: 'merchant_at_risk' | 'new_lead'
  item_id: string
  name: string
  value: number
  channel: string | null
  why: string[]
  monthly_paise: number | null
  diagnosis: string | null
  p: Record<string, number>
}
export interface WorkView { items: WorkItem[]; counts: { merchant_at_risk: number; new_lead: number }; assumptions: { p_lose: Record<string, number>; p_save: Record<string, number>; cost_paise: Record<string, number>; days_per_month: number } }

export interface Briefing { text: string; source: string }
export interface SavePlan {
  merchant_id: string
  eligible: boolean
  refusal: string | null
  approach: 'rep_visit' | 'offer' | null
  offer_months: number
  band_months: number
  signoff: string
  reasons: string[]
  briefing: Briefing[]
}
export interface Attempt { attempt_id: string; merchant_id: string; approach: string; offer_months: number | null; signoff: string; signed_by: string | null; status: string; diagnosis: string; created_at: string; closed_at: string | null; outcome: string | null }
export interface SaveView { merchant_id: string; name: string | null; diagnosis: string | null; monthly_paise: number; plan: SavePlan; attempt: Attempt | null }

export interface Sighting { sighting_id: string; merchant_id: string; rep_id: string; photo_ref: string; network: string | null; confidence: number; status: string; reason: string | null; seen_at: string; reviewed_by: string | null }
export interface SightingsView { sightings: Sighting[]; rivals: Record<string, string>; min_confidence: number }

export interface VoiceNote { language: string; script: string; words: number; seconds_estimate: number; translated: boolean; audio_made: boolean }
export interface EmailDraft { subject: string; lines: { text: string; source: string }[]; disclosure: string }
export interface StyleView {
  lead_id: string
  name: string
  value: number | null
  approach: { level: string; base_level: string; bumped: boolean; reasons: string[]; words: string }
  style: { kind: string; owner: string; language: string; why: string[]; steps: string[]; voice_note: VoiceNote | null; email: EmailDraft | null; gaps: string[] }
  voice_budget_words: number
}

export interface IncentiveLine { lead_id: string; merchant_id: string | null; merchant_name: string | null; visit_id: string; status: string; first_settlement_at: string | null; earned_paise: number; pending_paise: number; months: { month: number; status: string; volume_paise: number | null; earned_paise: number; share_paise: number }[]; note: string }
export interface RepView {
  rep_id: string
  name: string
  languages: Record<string, string>
  experience_months: number
  win_rates: Record<string, number>
  statement: { rep_id: string; lines: IncentiveLine[]; earned_paise: number; pending_paise: number; assumptions: Record<string, number> }
}
export interface RepsView { reps: RepView[]; briefing: { new_months: number | null; senior_months: number | null } }

export const getDips = () => call<DipsView>('GET', '/api/sales/dips')
export const getWork = () => call<WorkView>('GET', '/api/sales/work')
export const getSave = (mid: string) => call<SaveView>('GET', `/api/sales/save/${encodeURIComponent(mid)}`)
export const startSave = (mid: string) => call<{ attempt: Attempt; plan: SavePlan }>('POST', `/api/sales/save/${encodeURIComponent(mid)}/attempt`)
export const signOff = (mid: string, role: string, who: string) => call<{ attempt: Attempt }>('POST', `/api/sales/save/${encodeURIComponent(mid)}/sign-off`, { role, who })
export const applyOffer = (mid: string) => call<{ status: string; error: string | null; action_id: string | null }>('POST', `/api/sales/save/${encodeURIComponent(mid)}/offer`)
export const closeSave = (mid: string, outcome: 'SAVED' | 'LET_GO') => call<{ attempt: Attempt }>('POST', `/api/sales/save/${encodeURIComponent(mid)}/close`, { outcome })
export const getSightings = () => call<SightingsView>('GET', '/api/sales/sightings')
export const postSighting = (b: { rep_id: string; merchant_id: string; photo_ref: string; qr_visible: boolean; network: string | null; confidence: number }) => call<Sighting>('POST', '/api/sales/sightings', b)
export const reviewSighting = (id: string, role: string, who: string, accept: boolean) => call<Sighting>('POST', `/api/sales/sightings/${encodeURIComponent(id)}/review`, { role, who, accept })
export const getStyle = (leadId: string) => call<StyleView>('GET', `/api/sales/style/${encodeURIComponent(leadId)}`)
export const getReps = () => call<RepsView>('GET', '/api/sales/reps')

export const paise = (p: number): string => `₹${(p / 100).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
export const words = (s: string | null): string => (s ?? 'unknown').replace(/_/g, ' ').toLowerCase()
export const APPROACH_WORD: Record<string, string> = { rep_visit: 'A rep visits, with a briefing', offer: 'An offer inside the approved limit' }

export interface DeskHistory { from_state: string; to_state: string; reason: string | null; ts: string }
export interface DeskNote { note_id: number; rep_id: string | null; text: string; ts: string }
export interface DeskView {
  lead_id: string
  name: string
  state: string
  matched_merchant_id: string | null
  do_not_contact: boolean
  consented_channels: string[]
  can_send_first_contact: boolean
  next_states: string[]
  history: DeskHistory[]
  notes: DeskNote[]
}
export interface ContactResult { lead_id: string; status: string; channel: string | null; reasons: string[]; rule_ids: string[]; body: string | null; action_id: string | null }
export const getDesk = (leadId: string) => call<DeskView>('GET', `/api/sales/leads/${encodeURIComponent(leadId)}/desk`)
export const sendContact = (leadId: string) => call<ContactResult>('POST', `/api/sales/leads/${encodeURIComponent(leadId)}/contact`)
export const moveLead = (leadId: string, to: string, reason?: string) => call<{ lead_id: string; state: string; status: string }>('POST', `/api/sales/leads/${encodeURIComponent(leadId)}/move`, { to, reason })
export const addNote = (leadId: string, text: string, repId?: string) => call<{ note_id: number; text: string }>('POST', `/api/sales/leads/${encodeURIComponent(leadId)}/note`, { text, rep_id: repId })
