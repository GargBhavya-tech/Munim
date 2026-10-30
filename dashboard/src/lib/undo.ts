import type { ActionRow } from '../api/types'

export interface UndoState {
  enabled: boolean
  remainingMs: number | null
  label: string
}

/** Countdown is in SIM time: compensable_until minus the current simulated clock. The server still decides; this only shapes the button. */
export function undoState(a: Pick<ActionRow, 'undoable' | 'compensable_until' | 'status'>, simTimeIso: string | null): UndoState {
  if (!a.compensable_until) return { enabled: false, remainingMs: null, label: 'Not undoable' }
  if (!simTimeIso) return { enabled: false, remainingMs: null, label: 'Sim time unknown' }
  const remainingMs = Date.parse(a.compensable_until) - Date.parse(simTimeIso)
  if (!(remainingMs > 0)) return { enabled: false, remainingMs: 0, label: 'Undo window closed' }
  if (!a.undoable) return { enabled: false, remainingMs, label: 'Not undoable' }
  return { enabled: true, remainingMs, label: `Undo (${formatRemaining(remainingMs)} left, sim time)` }
}

export function formatRemaining(ms: number): string {
  const totalMin = Math.max(0, Math.floor(ms / 60000))
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

/** "6 PM" style clock text in IST from an ISO string with offset (uses the literal wall digits in the string). */
export function clockText(iso: string): string {
  const m = /T(\d{2}):(\d{2})/.exec(iso)
  if (!m) return iso
  let h = Number(m[1])
  const ap = h >= 12 ? 'PM' : 'AM'
  h = h % 12 || 12
  return `${h}:${m[2]} ${ap}`
}
