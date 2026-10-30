export const TWIN_BANNER = 'GUARDS OFF - SIMULATED TWIN'
export interface Reply { case_id: string; language: string; text: string }
export interface TwinSide { banner: string; label: string; replies: Reply[]; actions: { action: string; status: string; cls: string | null }[]; nonexistent_actions_requested: string[] }
export interface FanOut { banner: string; label: string; real: { replies: Reply[] }; twin: TwinSide }
