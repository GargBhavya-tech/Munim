import { useEffect, useRef, useState } from 'react'

/** True for `ms` after `value` changes (structural compare). Never true on first render. */
export function useFlash(value: unknown, ms = 1800): boolean {
  const sig = JSON.stringify(value) ?? ''
  const prev = useRef(sig)
  const [on, setOn] = useState(false)
  useEffect(() => {
    if (prev.current === sig) return
    prev.current = sig
    setOn(true)
    const t = setTimeout(() => setOn(false), ms)
    return () => clearTimeout(t)
  }, [sig, ms])
  return on
}
