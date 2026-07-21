import { useMemo, useState } from 'react'
import { useApi } from '../hooks/useApi'
import { apiText, errorMessage } from '../api/client'
import type { AppIssue, IssueLevel, IssueService, IssuesResponse } from '../api/types'
import { useSettings } from '../lib/settings'
import { formatDateTime } from '../lib/format'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

const SERVICES: Array<IssueService | ''> = ['', 'api', 'prediction', 'frontend']
const LEVELS: Array<IssueLevel | ''> = ['', 'error', 'warning']
const WINDOWS: Array<{ hours: number; label: string }> = [
  { hours: 24, label: '24 h' },
  { hours: 72, label: '3 days' },
  { hours: 168, label: '7 days' },
  { hours: 720, label: '30 days' }
]

function detailsText(details: Record<string, unknown> | null): string | null {
  if (!details) return null
  const entries = Object.entries(details)
  if (entries.length === 0) return null
  return entries
    .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join('\n')
}

export default function Issues() {
  const { calendar } = useSettings()
  const [service, setService] = useState<IssueService | ''>('')
  const [level, setLevel] = useState<IssueLevel | ''>('')
  const [hours, setHours] = useState(72)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [copyState, setCopyState] = useState<'idle' | 'copying' | 'copied' | 'failed'>('idle')

  const path = useMemo(() => {
    const params = new URLSearchParams({ limit: '300', since_hours: String(hours) })
    if (service) params.set('service', service)
    if (level) params.set('level', level)
    return `/issues?${params.toString()}`
  }, [service, level, hours])

  const issues = useApi<IssuesResponse>(path)
  const items: AppIssue[] = issues.data?.items ?? []
  const errorCount = items.filter((i) => i.level === 'error').length

  const copyReport = async () => {
    setCopyState('copying')
    try {
      const report = await apiText('/issues/report')
      await navigator.clipboard.writeText(report)
      setCopyState('copied')
      window.setTimeout(() => setCopyState('idle'), 2500)
    } catch (err) {
      // Clipboard can be unavailable (non-HTTPS); fall back to a download.
      try {
        const report = await apiText('/issues/report')
        const blob = new Blob([report], { type: 'text/markdown' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = 'issue-report.md'
        a.click()
        URL.revokeObjectURL(url)
        setCopyState('copied')
        window.setTimeout(() => setCopyState('idle'), 2500)
      } catch {
        console.error('report failed:', errorMessage(err))
        setCopyState('failed')
        window.setTimeout(() => setCopyState('idle'), 2500)
      }
    }
  }

  return (
    <div className="page-body">
      <h2 className="page-title">Issues</h2>
      <p className="muted">
        Warnings and errors collected from the Go API, the prediction service and the
        dashboard itself. Use <strong>Copy debug report</strong> to grab a Markdown digest you
        can paste into a debugging conversation (e.g. with Claude).
      </p>

      <div className="card">
        <div className="toolbar" style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
          <select value={service} onChange={(e) => setService(e.target.value as IssueService | '')} aria-label="Service filter">
            {SERVICES.map((s) => (
              <option key={s} value={s}>
                {s === '' ? 'All services' : s}
              </option>
            ))}
          </select>
          <select value={level} onChange={(e) => setLevel(e.target.value as IssueLevel | '')} aria-label="Level filter">
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l === '' ? 'All levels' : l}
              </option>
            ))}
          </select>
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))} aria-label="Time window">
            {WINDOWS.map((w) => (
              <option key={w.hours} value={w.hours}>
                last {w.label}
              </option>
            ))}
          </select>
          <button type="button" className="btn btn-ghost btn-sm" onClick={issues.reload}>
            Refresh
          </button>
          <span style={{ flex: 1 }} />
          <span className="muted small">
            {items.length} shown · {errorCount} errors
          </span>
          <button type="button" className="btn btn-sm" onClick={copyReport} disabled={copyState === 'copying'}>
            {copyState === 'copying'
              ? 'Building…'
              : copyState === 'copied'
                ? 'Copied ✓'
                : copyState === 'failed'
                  ? 'Failed — retry'
                  : 'Copy debug report'}
          </button>
        </div>
      </div>

      {issues.loading ? (
        <Loading label="Loading issues…" />
      ) : issues.error ? (
        <ErrorMessage message={issues.error} onRetry={issues.reload} />
      ) : items.length === 0 ? (
        <EmptyState
          title="No issues recorded"
          hint="Warnings and errors from all services will appear here as they happen."
        />
      ) : (
        <div className="card">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Service</th>
                  <th>Level</th>
                  <th>Source</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {items.map((issue) => {
                  const details = detailsText(issue.details)
                  const isOpen = expanded === issue.id
                  return (
                    <tr
                      key={issue.id}
                      onClick={() => setExpanded(isOpen ? null : issue.id)}
                      style={{ cursor: details ? 'pointer' : 'default' }}
                      title={details ? 'Click to toggle details' : undefined}
                    >
                      <td className="mono small">{formatDateTime(issue.occurred_at, calendar)}</td>
                      <td>
                        <span className="badge">{issue.service}</span>
                      </td>
                      <td>
                        <span className={`badge ${issue.level === 'error' ? 'badge-bad' : 'badge-warn'}`}>
                          {issue.level}
                        </span>
                      </td>
                      <td className="mono small">{issue.source || '—'}</td>
                      <td>
                        {issue.message}
                        {isOpen && details && (
                          <pre className="small mono" style={{ whiteSpace: 'pre-wrap', marginTop: '0.5rem', opacity: 0.8 }}>
                            {details}
                          </pre>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
