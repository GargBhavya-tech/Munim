import { describe, expect, it, vi } from 'vitest'
import { redeemOnce } from './redeem'

describe('redeemOnce', () => {
  it('redeems a single-use token once even when the effect runs twice (React StrictMode)', async () => {
    const api = { phoneRedeem: vi.fn(async (t: string) => ({ token: t })) }
    const [a, b] = await Promise.all([redeemOnce(api, 'jt_1'), redeemOnce(api, 'jt_1')])
    expect(api.phoneRedeem).toHaveBeenCalledTimes(1)
    expect(a).toBe(b)
  })
  it('a different token is redeemed on its own', async () => {
    const api = { phoneRedeem: vi.fn(async (t: string) => ({ token: t })) }
    await redeemOnce(api, 'jt_2')
    await redeemOnce(api, 'jt_3')
    expect(api.phoneRedeem).toHaveBeenCalledTimes(2)
  })
})
