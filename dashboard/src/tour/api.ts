import type { TourProgress, TourStep } from './types'

export const tourApi = {
  async steps(): Promise<TourStep[]> { return ((await (await fetch('/api/judge/tour')).json()) as { steps: TourStep[] }).steps },
  async progress(sid: string): Promise<TourProgress> { return (await (await fetch(`/api/judge/tour/${sid}`)).json()) as TourProgress },
}
