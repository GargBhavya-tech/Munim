import type { BreakItCardData } from './types'

/** The break-it card: suggested attacks. A click hands the text to the caller, which sends it through the normal judge message route. */
export function BreakItCard({ cards, onTry }: { cards: BreakItCardData[]; onTry: (text: string, language: string) => void }) {
  return (
    <section aria-label="Break it" data-testid="break-it" className="rounded border-2 border-amber-600 p-3">
      <h2 className="font-bold">Try to break Munim</h2>
      <ul className="mt-2 flex flex-col gap-3">
        {cards.map((c) => (
          <li key={c.id} data-testid="break-card">
            <p className="font-semibold">{c.title}</p>
            <ul className="flex flex-wrap gap-2">
              {c.tries.map((t) => (
                <li key={`${t.language}-${t.text}`}>
                  <button type="button" data-testid="break-try" data-language={t.language} onClick={() => onTry(t.text, t.language)} className="rounded border px-2 py-1 text-left">
                    <span className="mr-1 text-xs uppercase">{t.language}</span>
                    {t.text}
                    {t.review && <span className="ml-1 text-xs text-slate-600" data-testid="review-flag"> ({t.review})</span>}
                  </button>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  )
}
