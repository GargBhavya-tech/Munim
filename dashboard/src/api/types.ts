// Hand-written from docs/API_CONTRACT.md "Shapes" and the endpoint list. Do not reshape.
export interface Case {
  case_id: string
  merchant_id: string
  lead_id: string | null
  playbook_id: string
  state: string
  lane: string
  language: string
  step: string | null
  touches: number
  opened_at: string
  closed_at: string | null
  resume_note: string | null
}

export interface LedgerEntry {
  seq: number
  case_id: string | null
  ts_sim: string
  actor: string
  role: string | null
  kind: string
  payload: Record<string, unknown>
  policy_hash: string | null
  model_id: string | null
  hash: string
}

export interface ActionRow {
  action_id: string
  name: string
  cls: string
  status: string
  idempotency_key: string
  compensable_until: string | null
  undoable: boolean
}

export type ClaimLevel = 'CORROBORATED' | 'SUPPORTED' | 'UNSUPPORTED' | 'CONTRADICTED'
export interface Claim {
  id: string
  text: string
  source: string
  level: ClaimLevel
  color: 'green' | 'amber' | 'red'
  reason: string | null
  rule_id: string | null
  // #63 evidence (optional: older VERIFY entries do not carry it)
  verify_seq?: number
  nli_label?: string | null
  nli_score?: number | null
  snippet?: string | null
  corroborated_by?: string[]
}

export interface Receipt {
  action_id: string
  action: string
  label: string
  cancel: string
  compensable_until: string | null
}

export interface RenderSentence {
  kind: string
  text: string
  claim_ids: string[]
  ok: boolean
  back_translation: string | null
}
export interface Render {
  verify_seq: number
  status: string
  language: string
  sentences: RenderSentence[]
}

export interface Message {
  ts_sim: string
  direction: 'IN' | 'OUT'
  language: string
  text: string
}

export interface Handoff {
  handoff_id: string
  case_id: string
  packet: Record<string, unknown>
  status: string
  owner_role: string
  deadline: string
  created_at: string
  verdict: string | null
}

export interface Payment {
  [k: string]: unknown
}
export interface Settlement {
  [k: string]: unknown
}

export interface SystemsSnapshot {
  kyc: Record<string, unknown>
  bank: Record<string, unknown>
  device: Record<string, unknown>
  payments: { latest: Payment[]; settlement: Settlement | null }
  crm: { open_cases: Case[]; focus: string | null; language: string }
  pricing: { band: Record<string, unknown>; waivers: unknown[] }
  scheduler: { follow_ups: unknown[]; bookings: unknown[] }
  tickets: unknown[]
  comms: unknown[]
  handoff_queue: { open: number }
}

export interface CaseDetail {
  case: Case
  timeline: LedgerEntry[]
  actions: ActionRow[]
  claims: Claim[]
  messages: Message[]
  renders?: Render[]
  receipts?: Receipt[]
}

export interface CostResponse {
  ai_inr: number
  human_baseline_inr: { low: number; mid: number; high: number }
  assumptions: string[]
  usage: { tokens_in: number; tokens_out: number }
}

export interface LedgerPage {
  entries: LedgerEntry[]
  next_after_seq: number
}
export interface VerifyResult {
  ok: boolean
  entries: number
  first_mismatch: number | null
  reason: string | null
}
export interface ApprovalSummary {
  approval_id: string
  case_id: string
  action: string
  status: string
  role_required: string | string[]
}
export interface ChaosSetting {
  system: string
  mode: string
  rate: number
  param: number | null
}
export interface ControlState {
  kill_switch: boolean
  sim_time: string
  chaos: ChaosSetting[]
}

// GET /approvals/{id}: built by core.actions.approval.build_approval_view (root path).
export interface ApprovalView {
  approval_id: string
  status: string
  action: string
  risk_class: string
  args: Record<string, unknown>
  summary: string
  case_id: string
  requested_by: unknown
  state_before: unknown
  state_after_if_approved: unknown
  rules_fired: unknown[]
  compensation: unknown
  evidence_ids: unknown[]
  policy_hash: string
  required_roles: string[]
  decisions: { by: string; role: string; verdict: string; at: string }[]
  agent_explanation: { label: string; text: string }
}

export interface UndoResult {
  status: string
  action_id: string
  error: string | null
  violations: { rule_id?: string; reason?: string; [k: string]: unknown }[]
}

export type Role = 'support_executive' | 'risk_reviewer' | 'sales_lead' | 'ops_admin' | 'auditor'
export const ROLES: Role[] = ['support_executive', 'risk_reviewer', 'sales_lead', 'ops_admin', 'auditor']

export interface Identity {
  principal_id: string
  role: Role
}

export type WsMessage =
  | { type: 'hello'; sim_time: string }
  | { type: 'ledger'; entry: LedgerEntry }

/** Server refusal (HTTP non-2xx) kept as data so the UI can show the server's own words. */
export interface ApiRefusal {
  httpStatus: number
  body: { status?: string; rule_id?: string; reason?: string; detail?: unknown } & Record<string, unknown>
}

export interface CoverageRow {
  playbook_id: string
  half: string | null
  supported: boolean
  why_not: string[]
  families: string[]
  dev: { n: number; passed: number }
  heldout: { n: number; passed: number; attempts: number; pass_k: number; pass_k_ok: boolean }
  safety: { n: number; passed: number }
}
export interface CoverageResponse {
  config_hash: string
  results_config_hash: string | null
  stale: boolean
  last_run: { run: number; recorded_at: string; config_hash: string } | null
  pass_k: number
  playbooks: CoverageRow[]
  supported_count: number
}
export interface WhyRule {
  rule_id: string
  title: string
  severity: string
  source: string
  reason: string | null
}
export interface WhyResponse {
  seq: number
  action: string | null
  allowed: boolean
  outcome: string
  rules: WhyRule[]
  summary: string
}

export interface ReplayResponse {
  case_id: string
  chain: { ok: boolean; entries: number; first_mismatch: number | null; reason: string | null }
  state: Record<string, unknown>
  state_hash: string
  steps: { seq: number; ts_sim: string; kind: string; actor: string; summary: string }[]
}

export interface LadderEntry {
  playbook_id: string
  level: 'L0' | 'L1' | 'L2'
  reason: string | null
  updated_at: string | null
}
export interface LadderResponse {
  label: string
  levels: Record<'L0' | 'L1' | 'L2', string>
  playbooks: LadderEntry[]
}

// ---- Judge Mode (#129-#136) ----
export interface PersonaCard { persona_id: string; title: string; merchant_id: string; description: string }
export interface BreakItCard { id: string; title: string; tries: { language: string; text: string; review?: string }[] }
export interface Scoreboard { attempts: number; blocked_claims: number; refused_actions: number; escalations: number; forbidden_executed: number; fine_marks: number; label: string }
export interface Reply { seq?: number | null; case_id?: string | null; language?: string | null; text: string }
export interface JudgeView {
  session: { session_id: string; persona_id: string; merchant_id: string; start_seq: number }
  scoreboard: Scoreboard
  usage?: Record<string, unknown>
  caps?: Record<string, unknown>
  replies?: Reply[]
  dropped?: boolean
}
export interface TourStep { id: string; title: string; instruction: string; fact?: string; proves?: string; clip_available?: boolean }
export interface TourProgress { steps: { id: string; done: boolean; evidence_seq?: number | null }[]; done: number; total: number; next: string | null }
export interface TwinStatus { banner: string; label: string; active: boolean; isolated: boolean }
export interface TwinReply {
  banner: string
  label: string
  real: { replies: Reply[] } & Partial<JudgeView>        // with a session_id the real half is the judge view (session, scoreboard, usage, caps)
  twin: { replies: Reply[]; actions: { action: string; status: string; cls: string }[]; nonexistent_actions_requested: string[] }
}
export interface ArenaLevel { id: string; title: string; goal?: string }
export interface LeaderRow { rank: number; nickname: string; breaks: number; blocked: number; attempts: number }
export interface ArenaInfo { label: string; state: string; levels: ArenaLevel[]; budget: { spent_paise: number; budget_paise: number }; leaderboard: LeaderRow[] }
export interface ArenaPlayer { token: string; nickname: string }
