import { describe, expect, it } from 'vitest'
import { ApiError } from './client'

describe('ApiError message', () => {
  it('reads the error code out of a FastAPI detail object instead of printing [object Object]', () => {
    expect(new ApiError({ httpStatus: 404, body: { detail: { error: 'ARENA_DISABLED', label: 'SIMULATED' } } }).message).toBe('ARENA_DISABLED')
    expect(new ApiError({ httpStatus: 403, body: { detail: { error: 'NOT_ALLOWED', detail: 'auditor only' } } }).message).toBe('NOT_ALLOWED: auditor only')
  })
  it('keeps a reason, a string detail, and falls back to the HTTP status', () => {
    expect(new ApiError({ httpStatus: 409, body: { reason: 'band is one month' } }).message).toBe('band is one month')
    expect(new ApiError({ httpStatus: 422, body: { detail: 'bad input' } }).message).toBe('bad input')
    expect(new ApiError({ httpStatus: 500, body: {} }).message).toBe('HTTP 500')
  })
})
