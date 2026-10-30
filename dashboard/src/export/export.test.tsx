import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ExportButton, exportUrl } from './ExportButton'

describe('ExportButton', () => {
  it('points at the session export in both formats and names the files', () => {
    render(<ExportButton sessionId="JS-12" />)
    expect(screen.getByTestId('export-html').getAttribute('href')).toBe('/api/export/judge/JS-12?format=html')
    expect(screen.getByTestId('export-json').getAttribute('href')).toBe('/api/export/judge/JS-12?format=json')
    expect(screen.getByTestId('export-json').getAttribute('download')).toBe('evidence-JS-12.json')
    expect(screen.getByTestId('export-buttons').textContent).toContain('SIMULATED')
  })
  it('falls back to the whole-run export without a session', () => {
    render(<ExportButton />)
    expect(screen.getByTestId('export-html').getAttribute('href')).toBe('/api/export/run?format=html')
    expect(exportUrl(null, 'json')).toBe('/api/export/run?format=json')
  })
})
