import type { Case } from '../api/types'

const PREFERRED = ['NEW', 'ACTIVE', 'WAITING', 'PARKED', 'NOT_NOW', 'ESCALATED', 'RETURNED_TO_AGENT', 'BLOCKED', 'DECLINED', 'RESOLVED']

export interface Column {
  state: string
  cases: Case[]
}

/** Columns come from the data; states are never hard-coded as a closed list. Known states sort first in lifecycle order, unknown ones after, alphabetical. */
export function groupByState(cases: Case[]): Column[] {
  const map = new Map<string, Case[]>()
  for (const c of cases) {
    const l = map.get(c.state)
    if (l) l.push(c)
    else map.set(c.state, [c])
  }
  const rank = (s: string) => {
    const i = PREFERRED.indexOf(s)
    return i < 0 ? PREFERRED.length : i
  }
  return [...map.entries()]
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
    .map(([state, cs]) => ({ state, cases: cs.slice().sort((x, y) => x.case_id.localeCompare(y.case_id)) }))
}

/** Apply a changed case (from a refetch or event) to the list: replaces by id or appends. */
export function upsertCase(cases: Case[], c: Case): Case[] {
  const i = cases.findIndex((x) => x.case_id === c.case_id)
  if (i < 0) return [...cases, c]
  const next = cases.slice()
  next[i] = c
  return next
}
