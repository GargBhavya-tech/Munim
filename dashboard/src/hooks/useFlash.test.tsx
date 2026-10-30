import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useFlash } from './useFlash'

describe('useFlash', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('is off on first render, on after a change, off after the duration', () => {
    const { result, rerender } = renderHook(({ v }) => useFlash(v, 1000), { initialProps: { v: { a: 1 } } })
    expect(result.current).toBe(false)
    rerender({ v: { a: 2 } })
    expect(result.current).toBe(true)
    act(() => void vi.advanceTimersByTime(999))
    expect(result.current).toBe(true)
    act(() => void vi.advanceTimersByTime(2))
    expect(result.current).toBe(false)
  })

  it('does not flash when a new object is structurally equal', () => {
    const { result, rerender } = renderHook(({ v }) => useFlash(v), { initialProps: { v: { a: 1 } } })
    rerender({ v: { a: 1 } })
    expect(result.current).toBe(false)
  })
})
