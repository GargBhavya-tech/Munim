import type {
  ActionRow, ApiRefusal, ApprovalSummary, ApprovalView, Case, CaseDetail, ControlState, CostResponse, Handoff, Identity,
  ArenaInfo, ArenaPlayer, BreakItCard, CoverageResponse, JudgeView, LadderResponse, LedgerPage, PersonaCard, TourProgress, TourStep, TwinReply, TwinStatus, SystemsSnapshot, UndoResult, VerifyResult, WhyResponse, ReplayResponse } from './types'
import { mockApi } from './mock'

/** The words to show for a refusal. FastAPI puts our errors in ``detail`` as an object ({error, detail?, reason?}); String() of that is "[object Object]". */
function refusalText(r: ApiRefusal): string {
  if (r.body.reason) return String(r.body.reason)
  const d = r.body.detail
  if (typeof d === 'string') return d
  if (d && typeof d === 'object') {
    const o = d as Record<string, unknown>
    if (o.reason) return String(o.reason)
    if (o.error) return o.detail ? `${String(o.error)}: ${String(o.detail)}` : String(o.error)
  }
  return `HTTP ${r.httpStatus}`
}

export class ApiError extends Error {
  refusal: ApiRefusal
  constructor(refusal: ApiRefusal) {
    super(refusalText(refusal))
    this.refusal = refusal
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  let data: unknown = null
  try {
    data = await res.json()
  } catch {
    data = {}
  }
  if (!res.ok) throw new ApiError({ httpStatus: res.status, body: (data ?? {}) as ApiRefusal['body'] })
  return data as T
}

const qs = (o: Record<string, string | number | undefined | null>) => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v))
  const s = p.toString()
  return s ? `?${s}` : ''
}

export interface Api {
  health(): Promise<{ ok: boolean; sim_time: string }>
  cases(f?: { state?: string; lane?: string; merchant_id?: string }): Promise<{ cases: Case[] }>
  caseDetail(id: string): Promise<CaseDetail>
  cost(id: string): Promise<CostResponse>
  systems(merchantId: string): Promise<SystemsSnapshot>
  ledger(afterSeq: number, limit: number, caseId?: string): Promise<LedgerPage>
  verify(): Promise<VerifyResult>
  replay(caseId: string): Promise<ReplayResponse>
  coverage(): Promise<CoverageResponse>
  ladder(): Promise<LadderResponse>
  personas(): Promise<{ personas: PersonaCard[] }>
  breakIt(): Promise<{ cards: BreakItCard[] }>
  judgeStart(personaId: string): Promise<JudgeView>
  judgeSession(sid: string): Promise<JudgeView>
  judgeMessage(sid: string, text: string, language?: string): Promise<JudgeView>
  judgeFine(sid: string): Promise<{ ok: boolean; scoreboard: JudgeView['scoreboard'] }>
  tourSteps(): Promise<{ steps: TourStep[] }>
  tourProgress(sid: string): Promise<TourProgress>
  twinStatus(): Promise<TwinStatus>
  twinStart(): Promise<TwinStatus>
  twinMessage(merchantId: string, text: string, sessionId?: string): Promise<TwinReply>
  arenaInfo(): Promise<ArenaInfo>
  arenaJoin(nickname: string): Promise<ArenaPlayer>
  arenaAttempt(token: string, text: string): Promise<{ replies?: { text: string }[]; cleared?: string[]; blocked?: number; [k: string]: unknown }>
  phoneRedeem(token: string): Promise<JudgeView>
  speakReply(seq: number, sessionId: string): Promise<ArrayBuffer>
  why(seq: number): Promise<WhyResponse>
  handoffs(status?: string): Promise<{ handoffs: Handoff[] }>
  approvals(status?: string): Promise<{ approvals: ApprovalSummary[] }>
  approval(id: string): Promise<ApprovalView>
  control(): Promise<ControlState>
  undo(actionId: string, who: Identity): Promise<UndoResult>
  returnHandoff(id: string, instruction: string, who: Identity): Promise<{ ok: boolean; status: string }>
  verdict(id: string, verdict: 'needed' | 'not_needed'): Promise<{ ok: boolean }>
  decide(id: string, verdict: 'approve' | 'reject', who: Identity): Promise<{ status: string; approval_id: string }>
  killSwitch(on: boolean): Promise<{ kill_switch: boolean }>
  advanceClock(hours: number): Promise<{ sim_time: string }>
  chaos(c: { system: string; mode: string; rate: number; param: number | null }): Promise<{ ok: true }>
  pay(merchantId: string, amountPaise: number): Promise<{ payment_id: string; status: string; settlement_id: string }>
}

export const httpApi: Api = {
  health: () => call('GET', '/api/health'),
  cases: (f = {}) => call('GET', `/api/cases${qs(f)}`),
  caseDetail: (id) => call('GET', `/api/cases/${encodeURIComponent(id)}`),
  cost: (id) => call('GET', `/api/cases/${encodeURIComponent(id)}/cost`),
  systems: (m) => call('GET', `/api/merchants/${encodeURIComponent(m)}/systems`),
  ledger: (after, limit, caseId) => call('GET', `/api/ledger${qs({ after_seq: after, limit, case_id: caseId })}`),
  verify: () => call('GET', '/api/ledger/verify'),
  replay: (id) => call('GET', `/api/cases/${encodeURIComponent(id)}/replay`),
  coverage: () => call('GET', '/api/coverage'),
  ladder: () => call('GET', '/api/ladder'),
  personas: () => call('GET', '/api/judge/personas'),
  breakIt: () => call('GET', '/api/judge/break-it'),
  judgeStart: (persona_id) => call('POST', '/api/judge/sessions', { persona_id }),
  judgeSession: (sid) => call('GET', `/api/judge/sessions/${encodeURIComponent(sid)}`),
  judgeMessage: (sid, text, language) => call('POST', `/api/judge/sessions/${encodeURIComponent(sid)}/messages`, { text, language }),
  judgeFine: (sid) => call('POST', `/api/judge/sessions/${encodeURIComponent(sid)}/feedback`, { verdict: 'fine' }),
  tourSteps: () => call('GET', '/api/judge/tour'),
  tourProgress: (sid) => call('GET', `/api/judge/tour/${encodeURIComponent(sid)}`),
  twinStatus: () => call('GET', '/api/twin'),
  twinStart: () => call('POST', '/api/twin/start', {}),
  twinMessage: (merchant_id, text, session_id) => call('POST', '/api/twin/messages', { merchant_id, text, session_id }),
  arenaInfo: () => call('GET', '/api/arena'),
  arenaJoin: (nickname) => call('POST', '/api/arena/players', { nickname }),
  arenaAttempt: (token, text) => call('POST', `/api/arena/players/${encodeURIComponent(token)}/attempts`, { text }),
  phoneRedeem: (token) => call('POST', '/api/judge/phone/redeem', { token }),
  speakReply: async (seq, sessionId) => {
    const res = await fetch(`/api/voice/speak/${seq}?session_id=${encodeURIComponent(sessionId)}`, { method: 'POST' })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.arrayBuffer()
  },
  why: (seq) => call('GET', `/api/why/${seq}`),
  handoffs: (status) => call('GET', `/api/handoffs${qs({ status })}`),
  approvals: (status = 'PENDING') => call('GET', `/api/approvals${qs({ status })}`),
  approval: (id) => call('GET', `/approvals/${encodeURIComponent(id)}`),
  control: () => call('GET', '/api/control'),
  undo: (id, who) => call('POST', `/api/actions/${encodeURIComponent(id)}/undo`, { principal_id: who.principal_id, role: who.role }),
  returnHandoff: (id, instruction, who) =>
    call('POST', `/api/handoffs/${encodeURIComponent(id)}/return`, { instruction, principal_id: who.principal_id, role: who.role }),
  verdict: (id, verdict) => call('POST', `/api/handoffs/${encodeURIComponent(id)}/verdict`, { verdict }),
  decide: (id, verdict, who) =>
    call('POST', `/approvals/${encodeURIComponent(id)}/decide`, { principal_id: who.principal_id, role: who.role, verdict }),
  killSwitch: (on) => call('POST', '/api/control/kill-switch', { on }),
  advanceClock: (h) => call('POST', '/api/control/clock', { advance_hours: h }),
  chaos: (c) => call('POST', '/api/control/chaos', c),
  pay: (m, a) => call('POST', '/api/control/pay', { merchant_id: m, amount_paise: a }),
}

export const isMockMode = (): boolean => {
  try {
    return new URLSearchParams(window.location.search).get('mock') === '1'
  } catch {
    return false
  }
}

export const pickApi = (): Api => (isMockMode() ? mockApi : httpApi)
export type { ActionRow }
