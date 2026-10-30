import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PolicyCompiler } from './PolicyCompiler'
import type { Draft } from './types'

const draft = (ok: boolean): Draft => ({
  text: 'block send_message', source: 'template', dsl: { template: 'block_action', action: 'send_message' }, rule_id: 'CUSTOM-AB12CD34', rule_hash: 'a'.repeat(64),
  summary: 'Block send_message for everyone', diff: { added: [], removed: [], lines: ['+ CUSTOM-AB12CD34 [BLOCK] Block send_message for everyone'], policy_hash_before: 'x', policy_hash_after: 'y' },
  dry_run: { probes_run: 130, changed: [{ probe: 'p', source: 'frozen', action: 'send_message', before: 'ALLOW', after: 'BLOCK', new_rules: ['CUSTOM-AB12CD34'] }], loosened: [], removed_rules: [], ok, policy_hash_before: 'x', policy_hash_after: 'y' },
  ok, errors: [], label: 'SIMULATED',
})

function setup(d: Draft | null) {
  const api: any = {
    compile: vi.fn(async () => (d ? { ok: true, draft: d } : { ok: false, error: { error: 'RELAXING_REQUEST', message: 'only tightens' } })),
    approve: vi.fn(async () => ({ ok: true })), revert: vi.fn(async () => ({ ok: true })), rules: vi.fn(),
  }
  render(<PolicyCompiler api={api} />)
  fireEvent.change(screen.getByTestId('policy-text'), { target: { value: 'x' } })
  fireEvent.click(screen.getByRole('button', { name: 'Compile' }))
  return api
}

describe('PolicyCompiler', () => {
  it('shows the diff lines and dry-run counts verbatim from the server', async () => {
    setup(draft(true))
    await waitFor(() => screen.getByTestId('policy-draft'))
    expect(screen.getByTestId('policy-diff').textContent).toBe('+ CUSTOM-AB12CD34 [BLOCK] Block send_message for everyone')
    expect(screen.getByTestId('policy-dryrun').textContent).toContain('130 probes, 1 changed, 0 weakened')
  })
  it('disables approve when the dry run weakened something and sends the exact draft when ok', async () => {
    const api = setup(draft(true))
    await waitFor(() => screen.getByTestId('policy-draft'))
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(api.approve).toHaveBeenCalledWith(expect.objectContaining({ rule_hash: 'a'.repeat(64) }), 'u-ops', 'ops_admin'))
  })
  it('keeps approve disabled for a not-ok draft', async () => {
    setup(draft(false))
    await waitFor(() => screen.getByTestId('policy-draft'))
    expect((screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('shows a rejection code and no draft', async () => {
    setup(null)
    await waitFor(() => screen.getByTestId('policy-error'))
    expect(screen.getByTestId('policy-error').textContent).toContain('RELAXING_REQUEST')
    expect(screen.queryByTestId('policy-draft')).toBeNull()
  })
})
