import { AnimatePresence, motion } from 'motion/react'
import { useEffect, useState } from 'react'
import { ease, useMotion } from '../motion'
import { Button, Waveform } from '../ui'

/** The first seconds of the demo: a Soundbox announces a payment (a sample line), a ledger rule draws, and the tagline appears word by word. Plays once per tab,
 *  can be skipped with a click or Escape, and does not play at all under reduced motion. */

const WORDS = ['Every', 'merchant', 'case,', 'finished.']

export function Opening({ onDone }: { onDone: () => void }) {
  const m = useMotion()
  const [step, setStep] = useState(0)
  const [gone, setGone] = useState(false)

  useEffect(() => {
    if (m.reduce) { onDone(); return }
    const t = [setTimeout(() => setStep(1), 1700), setTimeout(() => setStep(2), 3600), setTimeout(() => setGone(true), 4300)]
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') setGone(true) }
    window.addEventListener('keydown', key)
    return () => { t.forEach(clearTimeout); window.removeEventListener('keydown', key) }
  }, [])                                                                                    // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <AnimatePresence onExitComplete={onDone}>
      {!gone && !m.reduce && (
        <motion.div
          key="opening" role="dialog" aria-label="Opening"
          className="fixed inset-0 z-50 flex flex-col items-center justify-center bg-bg px-6 text-center text-fg"
          exit={{ opacity: 0 }} transition={{ duration: 0.5, ease: ease.inOut }}
        >
          <div className="text-label text-muted absolute left-6 top-6">Soundbox · sample announcement</div>
          <div className="absolute right-6 top-6"><Button className="min-h-10" onClick={() => setGone(true)}>Skip</Button></div>

          <div className="text-ink"><Waveform live bars={44} className="!h-16" /></div>
          <motion.p className="text-display mt-8" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.8, delay: 0.3, ease: ease.out }}>
            ₹240 प्राप्त हुए
          </motion.p>

          <div className="mt-10 h-24" aria-live="polite">
            {step >= 1 && (
              <div>
                <p className="text-h1">{WORDS.map((w, i) => (
                  <span key={w}>{i > 0 && ' '}<motion.span className="inline-block" initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, delay: i * 0.09, ease: ease.out }}>{w}</motion.span></span>
                ))}</p>
                <motion.p className="text-voice text-ink mt-2" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.5, duration: 0.6 }}>Every word, proven.</motion.p>
              </div>
            )}
          </div>
          <motion.span aria-hidden="true" className="absolute bottom-0 left-0 h-px bg-stamp" initial={{ width: 0 }} animate={{ width: step >= 2 ? '100%' : '70%' }} transition={{ duration: step >= 2 ? 0.7 : 3.2, ease: ease.inOut }} />
        </motion.div>
      )}
    </AnimatePresence>
  )
}
