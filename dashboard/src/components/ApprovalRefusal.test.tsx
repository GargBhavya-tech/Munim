import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { ApprovalRefusal } from './ApprovalRefusal'

describe('ApprovalRefusal', () => {
  it('shows the server rule id, status and reason verbatim', () => {
    const err = new ApiError({ httpStatus: 403, body: { status: 'REFUSED', rule_id: 'ROLE-001', reason: "role 'auditor' (HUMAN) may not decide this approval" } })
    render(<ApprovalRefusal error={err} />)
    const box = screen.getByTestId('refusal')
    expect(box.textContent).toContain('ROLE-001')
    expect(box.textContent).toContain('403')
    expect(box.textContent).toContain("role 'auditor' (HUMAN) may not decide this approval")
  })
  it('renders nothing without an error', () => {
    const { container } = render(<ApprovalRefusal error={null} />)
    expect(container.innerHTML).toBe('')
  })
})
