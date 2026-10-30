import type { LedgerEntry, LedgerPage } from '../api/types'

export interface PagerState {
  entries: LedgerEntry[] // only the pages already requested, never the whole ledger
  afterSeq: number
  done: boolean
  loading: boolean
  kinds: string[] // empty = all
  pageSize: number
}

export type PagerAction =
  | { type: 'reset'; kinds?: string[] }
  | { type: 'loading' }
  | { type: 'page'; page: LedgerPage; requestedLimit: number }
  | { type: 'toggleKind'; kind: string }

export const initialPager = (pageSize = 50): PagerState => ({ entries: [], afterSeq: 0, done: false, loading: false, kinds: [], pageSize })

export function pagerReducer(s: PagerState, a: PagerAction): PagerState {
  switch (a.type) {
    case 'reset':
      return { ...initialPager(s.pageSize), kinds: a.kinds ?? s.kinds }
    case 'loading':
      return { ...s, loading: true }
    case 'toggleKind': {
      const kinds = s.kinds.includes(a.kind) ? s.kinds.filter((k) => k !== a.kind) : [...s.kinds, a.kind]
      return { ...initialPager(s.pageSize), kinds }
    }
    case 'page': {
      const seen = new Set(s.entries.map((e) => e.seq))
      const fresh = a.page.entries.filter((e) => !seen.has(e.seq))
      return {
        ...s,
        entries: [...s.entries, ...fresh],
        afterSeq: Math.max(s.afterSeq, a.page.next_after_seq),
        done: a.page.entries.length < a.requestedLimit,
        loading: false,
      }
    }
  }
}

/** The contract has no kinds query param, so the filter is applied to the loaded page only. */
export const visibleEntries = (s: PagerState): LedgerEntry[] => (s.kinds.length ? s.entries.filter((e) => s.kinds.includes(e.kind)) : s.entries)
