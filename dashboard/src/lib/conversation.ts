import type { Render } from '../api/types'

/** OUT message text -> the gate's English back-translation of it (joined per sentence), from the case's VERIFY records.
 *  Matched by the exact text the gate checked; a message the gate never saw (e.g. a read-back prompt) has none. */
export function backTranslations(renders: Render[]): (text: string) => string | null {
  const map = new Map<string, string>()
  for (const r of renders) {
    if (!r.sentences.some((s) => s.back_translation)) continue
    const parts = r.sentences.map((s) => s.back_translation ?? (s.kind === 'claim' ? null : s.text))
    if (parts.some((p) => p === null)) continue
    map.set(r.sentences.map((s) => s.text).join(' '), parts.join(' '))
  }
  return (text) => map.get(text) ?? null
}

const VOICE_LANG: Record<string, string> = { 'hi-IN': 'hi-IN', hinglish: 'hi-IN', 'kn-IN': 'kn-IN', 'en-IN': 'en-IN', hi: 'hi-IN', kn: 'kn-IN', en: 'en-IN' }

/** Voice playback in the browser (demo). Raw merchant audio is never stored, so this speaks the stored text. */
export function speak(text: string, language: string): boolean {
  const synth = typeof window !== 'undefined' ? window.speechSynthesis : undefined
  if (!synth || typeof SpeechSynthesisUtterance === 'undefined') return false
  const u = new SpeechSynthesisUtterance(text)
  u.lang = VOICE_LANG[language] ?? 'en-IN'
  synth.cancel()
  synth.speak(u)
  return true
}
