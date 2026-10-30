/** Download links for the evidence pack (#136). The server builds and verifies the pack; nothing is computed here. */
export function exportUrl(sessionId: string | null | undefined, format: 'json' | 'html'): string {
  return sessionId ? `/api/export/judge/${encodeURIComponent(sessionId)}?format=${format}` : `/api/export/run?format=${format}`
}

export function ExportButton({ sessionId }: { sessionId?: string | null }) {
  const name = sessionId ? `evidence-${sessionId}` : 'evidence-run'
  return (
    <div data-testid="export-buttons" className="flex flex-wrap items-center gap-2 text-sm">
      <a data-testid="export-html" href={exportUrl(sessionId, 'html')} download={`${name}.html`} className="rounded border border-slate-500 px-3 py-1">
        Download evidence pack (HTML)
      </a>
      <a data-testid="export-json" href={exportUrl(sessionId, 'json')} download={`${name}.json`} className="rounded border border-slate-500 px-3 py-1">
        Download evidence pack (JSON)
      </a>
      <span className="text-xs text-slate-600">SIMULATED · verify offline with scripts/verify_pack.py</span>
    </div>
  )
}
