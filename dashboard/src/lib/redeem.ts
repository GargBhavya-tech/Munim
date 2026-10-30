/** A phone token is single-use. React StrictMode (dev) runs effects twice, and a remount runs them again, so the second redeem would fail with
 *  TOKEN_USED and show an error on a working phone. Every caller in this page gets the ONE redeem of a token. */
const redeemed = new Map<string, Promise<unknown>>()

export function redeemOnce<T>(api: { phoneRedeem(token: string): Promise<T> }, token: string): Promise<T> {
  let p = redeemed.get(token) as Promise<T> | undefined
  if (!p) {
    p = api.phoneRedeem(token)
    redeemed.set(token, p)
  }
  return p
}
