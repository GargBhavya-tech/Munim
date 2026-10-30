import { TWIN_BANNER } from './types'

/** Always rendered above the twin column, whatever the state. The text is a constant, not taken from a response. */
export function TwinBanner() {
  return (
    <div role="alert" data-testid="twin-banner" className="rounded bg-red-700 px-3 py-2 text-center text-sm font-bold uppercase tracking-wide text-white">
      {TWIN_BANNER}
    </div>
  )
}
