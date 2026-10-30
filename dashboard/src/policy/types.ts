/** Policy compiler contract (core/api/routes_policy_compile.py). Every value is computed by the server; nothing is derived here. */
export interface DryRunChange { probe: string; source: string; action: string; before: string; after: string; new_rules: string[] }
export interface DryRun { probes_run: number; changed: DryRunChange[]; loosened: DryRunChange[]; removed_rules: string[]; ok: boolean; policy_hash_before: string; policy_hash_after: string }
export interface Draft {
  text: string; source: 'template' | 'llm_proposal'; dsl: Record<string, unknown>; rule_id: string; rule_hash: string; summary: string
  diff: { added: unknown[]; removed: string[]; lines: string[]; policy_hash_before: string; policy_hash_after: string }
  dry_run: DryRun; ok: boolean; errors: { code: string; probe?: string }[]; label: 'SIMULATED'
}
export interface CompileError { error: string; message: string }
export interface CompiledRule { rule_id: string; rule_hash: string; dsl: Record<string, unknown>; text: string | null; seq: number }
