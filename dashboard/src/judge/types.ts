/** Judge Mode contract (docs/API_CONTRACT.md, "Judge Mode"). Every number is a ledger count computed by the server. */
export interface Scoreboard {
  attempts: number
  blocked_claims: number
  refused_actions: number
  escalations: number
  forbidden_executed: number
  fine_marks: number
  label: 'SIMULATED'
}
export interface JudgeUsage { messages: number; recent_messages: number; tokens: number; cost_paise: number }
export interface JudgeCaps { max_messages: number; max_tokens: number; max_cost_paise: number; burst_messages: number; burst_window_s: number }
export interface BreakItTry { language: 'en' | 'hi' | 'hinglish' | 'kn'; text: string; review?: string }
export interface BreakItCardData { id: string; title: string; tries: BreakItTry[] }
export interface CapRefusal { error: 'JUDGE_CAP'; cap: string; limit: number; used: number; reason: string }
