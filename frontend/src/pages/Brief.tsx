import { useEffect, useMemo } from 'react'
import { useApi } from '../hooks/useApi'
import { useCustomForecast } from '../hooks/useCustomForecast'
import type { MarketSummary, Prediction } from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import { formatDateTime, formatToman, relativeTime } from '../lib/format'
import { effectiveCostPct } from '../lib/advice'
import { normalizePrediction } from '../lib/forecastChart'
import { composeBrief } from '../lib/brief'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

const BRIEF_MC_DAYS = 7

/**
 * The Brief tab: the whole dashboard condensed into a written page — where
 * things stand, what the models expect, the simulated odds, and one
 * prescription paragraph. Composed client-side from the same payloads the
 * charts use (see lib/brief.ts), so it always matches them.
 */
export default function Brief() {
  const { unit, calendar } = useSettings()

  const summary = useApi<MarketSummary>('/market/summary')
  const latest = useApi<unknown>('/predictions')
  const predictions = useMemo(
    () => unwrapList<Prediction>(latest.data, 'items', 'predictions').map(normalizePrediction),
    [latest.data]
  )
  const custom = useCustomForecast()
  const { run } = custom
  useEffect(() => {
    run(BRIEF_MC_DAYS)
  }, [run])

  const costPct = effectiveCostPct(summary.data?.trading_cost_pct)
  const sections = useMemo(
    () =>
      composeBrief({
        summary: summary.data ?? null,
        predictions,
        custom: custom.result ?? null,
        costPct,
        fmt: (v: number) => formatToman(v, unit)
      }),
    [summary.data, predictions, custom.result, costPct, unit]
  )

  if (summary.loading && latest.loading) return <Loading label="Writing the brief…" />

  const asOf = summary.data?.last_update

  return (
    <div className="page-body">
      <h2 className="page-title">Brief</h2>

      {summary.error && <ErrorMessage message={summary.error} onRetry={summary.reload} />}
      {latest.error && !summary.error && (
        <ErrorMessage message={latest.error} onRetry={latest.reload} />
      )}

      {asOf && (
        <p className="muted small">
          Written from data as of {formatDateTime(asOf, calendar)} ({relativeTime(asOf)}).
        </p>
      )}

      {sections.length === 0 ? (
        <EmptyState
          title="Nothing to summarize yet"
          hint="The brief appears once prices and forecasts have been collected."
        />
      ) : (
        sections.map((s) => (
          <div className="card brief-section" key={s.key}>
            <div className="card-title">{s.title}</div>
            {s.paragraphs.map((p, i) => (
              <p className="brief-paragraph" key={i}>
                {p}
              </p>
            ))}
          </div>
        ))
      )}
    </div>
  )
}
