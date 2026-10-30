import type { CompileError, CompiledRule, Draft } from './types'

async function post<T>(path: string, body: unknown): Promise<{ status: number; data: T }> {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  return { status: res.status, data: (await res.json()) as T }
}

export type CompileResult = { ok: true; draft: Draft } | { ok: false; error: CompileError }

export const policyApi = {
  async compile(text: string): Promise<CompileResult> {
    const { status, data } = await post<any>('/api/policy/compile', { text })
    return status === 200 ? { ok: true, draft: data as Draft } : { ok: false, error: data as CompileError }
  },
  async approve(d: Draft, principalId: string, role: string): Promise<{ ok: boolean; error?: CompileError; policy_hash?: string }> {
    const { status, data } = await post<any>('/api/policy/approve', { dsl: d.dsl, rule_hash: d.rule_hash, text: d.text, principal_id: principalId, role })
    return status === 200 ? { ok: true, policy_hash: data.policy_hash } : { ok: false, error: data as CompileError }
  },
  async revert(ruleId: string, principalId: string, role: string): Promise<{ ok: boolean; error?: CompileError }> {
    const { status, data } = await post<any>('/api/policy/revert', { rule_id: ruleId, principal_id: principalId, role })
    return status === 200 ? { ok: true } : { ok: false, error: data as CompileError }
  },
  async rules(): Promise<{ policy_hash: string; compiled: CompiledRule[] }> {
    return (await (await fetch('/api/policy/rules')).json()) as { policy_hash: string; compiled: CompiledRule[] }
  },
}
