/** The service-ops API (core/api/routes_ops.py) as the Service ops screen uses it. Every number, name and time comes from the server: the fix plan's costs and chances, the
 *  routes, who is on call. Nothing here computes or ranks anything. Travel times behind the day plan are SIMULATED, and the responses say so. */

export interface Situation {
  merchant_id: string
  language: string
  device_status: string
  last_test_ok: boolean
  silent_hours: number | null
  remote_attempts: number
  replacements_30d: number
  chronic: boolean
  daily_volume_paise: number
  open_hours: number
  hours_source: string
}

export interface RungView { rung: number; name: string; cost_paise: number; hours: number; p_success: number; expected_if_start_here_paise: number }
export interface FixPlan { merchant_id: string; start_rung: number; rungs: RungView[]; loss_per_hour_paise: number; swap: boolean; reasons: string[]; assumptions: Record<string, unknown> }

export interface Step {
  rung: number
  who?: string | null
  person_id?: string
  name?: string
  mode?: string
  eta_min?: number
  cost_paise?: number
  why?: string[]
  does?: string[]
  then?: string
  unavailable?: string
  note?: string
  rejected?: Record<string, string>
  booking?: { action: string; args: { merchant_id: string; kind: string; slot_at: string; person_id?: string } }
}

export interface FixPlanView { situation: Situation; plan: FixPlan; people: { start_rung: number; steps: Step[] }; simulated: boolean }

export interface PlannedStop { job_id: string; merchant_id: string; name: string; arrive_min: number; leave_min: number; lat: number; lng: number; late_min: number }
export interface Rescheduled { booking_id: string; merchant_id: string; name: string; person_id: string | null; reason: string; proposed_slot_at: string }
export interface DayView {
  day: string
  routes: Record<string, PlannedStop[]>
  unassigned: { job_id: string; merchant_id: string; reason: string; rejected?: Record<string, string> }[]
  rescheduled: Rescheduled[]
  delayed: Record<string, number>
  starts: Record<string, string>
  simulated: boolean
}

export interface Tech { tech_id: string; name: string; languages: Record<string, string>; skills: string[]; certifications: string[]; parts: Record<string, number>; shift: [string, string]; base: [number, number]; max_jobs_per_day: number }
export interface Helper { helper_id: string; name: string; languages: Record<string, string>; modes: string[]; on_call: [string, string]; skills: string[] }
export interface Courier { courier_id: string; name: string; base: [number, number]; shift: [string, string]; units: number }
export interface Position { person_id: string; name: string | null; kind: string; lat: number; lng: number; ts: string; age_min: number }
export interface PeopleView { technicians: Tech[]; helpers: Helper[]; couriers: Courier[]; positions: Position[]; sim_time: string }

export interface Hours { open: string; close: string; closed_days: string[]; source: string }
export interface Signal { code: string; severity: string; text: string }
export interface ScamReport { merchant_id: string; verdict: string; signals: Signal[]; copied: { payments: number; references: number }; discarded: boolean }

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, { method, headers: body === undefined ? undefined : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
  if (r.ok) return r.json()
  let msg = `HTTP ${r.status}`
  try {
    const d = (await r.json())?.detail
    msg = typeof d === 'string' ? d : d?.reason ? `${d.error}: ${d.reason}` : Array.isArray(d) ? d.map((x: { msg?: string }) => x.msg).join('; ') : d?.error ?? msg
  } catch { /* not JSON: the status is the message */ }
  throw new Error(msg)
}

export const getFixPlan = (mid: string) => call<FixPlanView>('GET', `/api/ops/fix-plan/${encodeURIComponent(mid)}`)
export const bookStep = (mid: string, rung: number) => call<{ status: string; error: string | null; action_id: string | null; step: Step }>('POST', `/api/ops/fix-plan/${encodeURIComponent(mid)}/book`, { rung })
export const getDay = () => call<DayView>('GET', '/api/ops/day')
export const postException = (body: { kind: 'SHOP_CLOSED' | 'RUNNING_LONG'; booking_id?: string; person_id?: string; minutes?: number }) =>
  call<{ exception: unknown; plan: DayView }>('POST', '/api/ops/day/exception', body)
export const reschedule = (booking_id: string) => call<{ status: string; error: string | null; proposal: Rescheduled }>('POST', '/api/ops/day/reschedule', { booking_id })
export const getPeople = () => call<PeopleView>('GET', '/api/ops/people')
export const getHours = (mid: string) => call<Hours>('GET', `/api/ops/shop-hours/${encodeURIComponent(mid)}`)
export const putHours = (mid: string, h: { open: string; close: string; closed_days: string[] }) => call<Hours>('PUT', `/api/ops/shop-hours/${encodeURIComponent(mid)}`, h)
export const scamCheck = (b: { merchant_id: string; amount_paise: number; reference?: string; claimed_at?: string }) => call<ScamReport>('POST', '/api/ops/scam-check', b)

export const hhmm = (minute: number): string => `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`
export const rupeesFromPaise = (p: number): string => `₹${(p / 100).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
export const RUNG_WORD: Record<number, string> = { 0: 'Nothing to fix', 1: 'Rung 1: Munim fixes it', 2: 'Rung 2: a helper guides', 3: 'Rung 3: someone goes' }
