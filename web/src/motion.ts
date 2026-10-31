import { useReducedMotion } from 'motion/react'

/** Motion tokens (docs/FRONTEND_PLAN.md section 3). Motion must guide attention, show state, or keep spatial continuity; otherwise it is removed.
 *  Only transform and opacity are animated. Every motif runs once, on a state change, never as an idle loop. */
export const ease = {
  out: [0.16, 1, 0.3, 1] as [number, number, number, number],      // everything entering
  inOut: [0.65, 0, 0.35, 1] as [number, number, number, number],   // view changes, the verify sweep
}
export const dur = { fast: 0.14, base: 0.28, slow: 0.6, scene: 1.2 }
export const dist = { sm: 8, md: 16, lg: 24 }
export const STAGGER = 0.07            // never above 0.1: a longer stagger feels slow

/** A weak device (few cores, or little memory where the browser says so) gets half the duration, so the demo never stutters on venue hardware. */
export function isLowEnd(): boolean {
  if (typeof navigator === 'undefined') return false
  const n = navigator as Navigator & { deviceMemory?: number }
  if (n.deviceMemory !== undefined) return n.deviceMemory <= 2
  return (n.hardwareConcurrency ?? 8) <= 4
}

const LOW = isLowEnd()

/** One place that applies reduced motion and the low-end rule. `reduce` means: no travel, near-instant, no sweeps, no waveform movement. */
export function useMotion() {
  const reduce = !!useReducedMotion()
  const scale = reduce ? 0.01 : LOW ? 0.5 : 1
  return {
    reduce,
    /** seconds, scaled */
    d: (seconds: number) => seconds * scale,
    /** pixels of travel, zero under reduced motion */
    y: (px: number) => (reduce ? 0 : px),
    /** props for an element that enters once */
    enter: (delay = 0, travel: number = dist.md) => ({
      initial: { opacity: 0, y: reduce ? 0 : travel },
      animate: { opacity: 1, y: 0 },
      transition: { duration: dur.slow * scale, delay: reduce ? 0 : delay, ease: ease.out },
    }),
  }
}
