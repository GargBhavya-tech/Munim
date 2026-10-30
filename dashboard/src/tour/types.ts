export interface TourStep { id: string; title: string; ui_element: string; ui_label?: string; instruction: string; proves: string; clip: string; clip_available: boolean }
export interface TourProgressStep { id: string; done: boolean; metric: string; value: number; op: string; needed: number; evidence_seq: number | null }
export interface TourProgress { steps: TourProgressStep[]; done: number; total: number; next: string | null; label: 'SIMULATED' }
