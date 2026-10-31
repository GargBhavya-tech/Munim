import { AnimatePresence, motion } from 'motion/react'
import { useState } from 'react'
import { GUIDES } from './guides'
import { dur, ease, useMotion } from './motion'

/** The strip under the header that says what a screen is for, what to try, and what good looks like. Open the first time, then folded to one button
 *  (remembered per screen; storage can be blocked, in which case it simply starts open each visit). */

const store = (id: string) => `munim-guide-${id}`
const wasFolded = (id: string) => { try { return localStorage.getItem(store(id)) === 'folded' } catch { return false } }
const remember = (id: string, folded: boolean) => { try { localStorage.setItem(store(id), folded ? 'folded' : 'open') } catch { /* blocked storage: nothing to remember */ } }

const LABELS = [
  ['see', 'What you are looking at'],
  ['try', 'Try this'],
  ['good', 'What good looks like'],
] as const

export function ScreenGuide({ id }: { id: string }) {
  const guide = GUIDES[id]
  const m = useMotion()
  const [folded, setFolded] = useState(() => wasFolded(id))
  if (!guide) return null
  const set = (v: boolean) => { setFolded(v); remember(id, v) }
  return (
    <div className="mx-auto max-w-6xl px-6 pt-6">
      <AnimatePresence mode="wait" initial={false}>
        {folded ? (
          <motion.div key="folded" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: m.d(dur.fast), ease: ease.out }}>
            <button type="button" onClick={() => set(false)} aria-expanded="false" className="text-label inline-flex min-h-9 items-center gap-2 border border-line px-3 text-muted hover:text-fg">
              About this screen <span aria-hidden="true">+</span>
            </button>
          </motion.div>
        ) : (
          <motion.section
            key="open"
            aria-label="About this screen"
            className="border border-line bg-surface p-4"
            initial={{ opacity: 0, y: m.y(6) }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: m.d(dur.base), ease: ease.out }}
          >
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-label text-muted">About this screen</h2>
              <button type="button" onClick={() => set(true)} aria-expanded="true" className="text-label min-h-9 px-2 text-muted hover:text-fg">{'Hide −'}</button>
            </div>
            <dl className="mt-3 grid gap-x-8 gap-y-4 md:grid-cols-3">
              {LABELS.map(([k, label]) => (
                <div key={k}>
                  <dt className="text-label text-ink">{label}</dt>
                  <dd className="text-small mt-1">{guide[k]}</dd>
                </div>
              ))}
            </dl>
          </motion.section>
        )}
      </AnimatePresence>
    </div>
  )
}
