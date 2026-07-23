import { useMemo } from 'react'
import { useApi } from '../hooks/useApi'
import type {
  CurrentPricesResponse,
  FundsResponse,
  MarketSummary,
  PortfolioResponse,
  Prediction,
  PriceHistoryResponse
} from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import {
  currencyCode,
  formatDateTime,
  formatPct,
  formatTime,
  formatToman,
  formatUsd,
  shortDate
} from '../lib/format'
import { effectiveCostPct } from '../lib/advice'
import { buildForecastChartData, normalizePrediction } from '../lib/forecastChart'
import StatCard from '../components/StatCard'
import DataFreshness from '../components/DataFreshness'
import ProviderStatus from '../components/ProviderStatus'
import ProviderGapCard from '../components/ProviderGapCard'
import GoldFundsPanel from '../components/GoldFundsPanel'
import AdvisorPanel from '../components/AdvisorCard'
import ActionPlannerPanel from '../components/ActionPlanner'
import PriceChart, { type ChartPoint } from '../components/PriceChart'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

const DAY_MS = 24 * 60 * 60 * 1000

export default function Overview() {
  const { unit, calendar } = useSettings()

  const summary = useApi<MarketSummary>('/market/summary')
  const current = useApi<CurrentPricesResponse>('/prices/current')
  const latest = useApi<unknown>('/predictions')
  const predictions = useMemo(
    () => unwrapList<Prediction>(latest.data, 'items', 'predictions').map(normalizePrediction),
    [latest.data]
  )
  const historyPath = useMemo(() => {
    const from = new Date(Date.now() - 30 * DAY_MS).toISOString()
    return `/prices/history?symbol=IR_GOLD_18K&interval=daily&page_size=500&from=${encodeURIComponent(from)}`
  }, [])
  const history = useApi<PriceHistoryResponse>(historyPath)
  // Lifted fetches, shared by AdvisorPanel / ActionPlanner / GoldFundsPanel.
  const portfolio = useApi<PortfolioResponse>('/portfolio')
  const funds = useApi<FundsResponse>('/market/funds')

  const chartData: ChartPoint[] = useMemo(() => {
    const merged = buildForecastChartData(history.data?.items ?? [], predictions)
    return merged.map((p) => {
      const d = new Date(p.t)
      // Daily labels for actuals; forecast-only targets can be intraday.
      const label =
        p.actual !== undefined ? shortDate(d, calendar) : `${shortDate(d, calendar)} ${formatTime(d)}`
      return { label, actual: p.actual, forecast: p.forecast, band: p.band }
    })
  }, [history.data, predictions, calendar])

  if (summary.loading && current.loading) return <Loading label="Loading market overview…" />

  const s = summary.data
  const prices = current.data?.prices
  const gold = prices?.IR_GOLD_18K
  const xau = prices?.XAUUSD
  const usdIrt = prices?.USD_IRT

  const goldValue = gold?.value ?? s?.current_18k?.value ?? null
  const goldChange = gold?.change_24h_pct ?? s?.current_18k?.change_24h_pct ?? null
  const premiumDeviation =
    s && s.premium_pct != null && s.premium_avg_30d != null
      ? Math.abs(s.premium_pct - s.premium_avg_30d)
      : null
  const premiumAbnormal = premiumDeviation !== null && premiumDeviation >= 2

  return (
    <div className="page-body">
      <h2 className="page-title">Market overview</h2>

      {summary.error && <ErrorMessage message={summary.error} onRetry={summary.reload} />}
      {current.error && !summary.error && (
        <ErrorMessage message={current.error} onRetry={current.reload} />
      )}

      <div className="grid">
        <StatCard
          label={`18k gold — ${currencyCode(unit)} per gram`}
          value={
            goldValue !== null ? (
              <span className="big-price">{formatToman(goldValue, unit, false)}</span>
            ) : (
              '—'
            )
          }
          delta={goldChange}
          sub={
            gold ? (
              <DataFreshness
                timestamp={gold.observed_at}
                stale={gold.stale}
                marketState={gold.market_state}
              />
            ) : (
              <span className="muted small">no observation</span>
            )
          }
          tone="accent"
        />
        <StatCard
          label="Global gold (XAU/USD, per ozt)"
          value={xau ? formatUsd(xau.value) : s?.xau_usd ? formatUsd(s.xau_usd.value) : '—'}
          delta={xau?.change_24h_pct}
          sub={
            xau && (
              <DataFreshness
                timestamp={xau.observed_at}
                stale={xau.stale}
                marketState={xau.market_state}
              />
            )
          }
        />
        <StatCard
          label={`USD / ${currencyCode(unit)} (free market)`}
          value={
            usdIrt
              ? formatToman(usdIrt.value, unit)
              : s?.usd_irt
                ? formatToman(s.usd_irt.value, unit)
                : '—'
          }
          delta={usdIrt?.change_24h_pct}
          sub={
            usdIrt && (
              <DataFreshness
                timestamp={usdIrt.observed_at}
                stale={usdIrt.stale}
                marketState={usdIrt.market_state}
              />
            )
          }
        />
        <StatCard
          label="Premium over theoretical"
          value={
            s ? (
              <span className={premiumAbnormal ? 'warn-text' : undefined}>
                {formatPct(s.premium_pct)}
              </span>
            ) : (
              '—'
            )
          }
          tone={premiumAbnormal ? 'warn' : 'default'}
          sub={
            s ? (
              <>
                <div className="kv">
                  <span className="muted">Theoretical</span>
                  <span className="mono">
                    {s.theoretical_18k != null ? formatToman(s.theoretical_18k, unit) : '—'}
                  </span>
                </div>
                <div className="kv">
                  <span className="muted">Observed</span>
                  <span className="mono">{s.current_18k ? formatToman(s.current_18k.value, unit) : '—'}</span>
                </div>
                <div className="kv">
                  <span className="muted">30d avg premium</span>
                  <span className="mono">{formatPct(s.premium_avg_30d)}</span>
                </div>
                {premiumAbnormal && (
                  <div className="warn-text small">
                    Premium deviates {premiumDeviation?.toFixed(1)} pp from its 30-day average —
                    local market is unusually {(s.premium_pct ?? 0) > (s.premium_avg_30d ?? 0) ? 'expensive' : 'cheap'}
                    {' '}versus global parity.
                  </div>
                )}
              </>
            ) : undefined
          }
        />
      </div>

      <AdvisorPanel
        signal={s?.signal ?? null}
        predictions={predictions}
        currentPrice={goldValue}
        premiumPct={s?.premium_pct ?? null}
        costPct={effectiveCostPct(s?.trading_cost_pct)}
        loading={summary.loading}
        portfolio={portfolio.data}
      />

      <ActionPlannerPanel
        predictions={predictions}
        history={history.data?.items ?? []}
        loading={latest.loading || history.loading}
        currentPrice={goldValue}
        premiumPct={s?.premium_pct ?? null}
        premiumAvg30d={s?.premium_avg_30d ?? null}
        fundsFlowPct={funds.data?.flow_pct ?? null}
        portfolio={portfolio.data}
        costPct={effectiveCostPct(s?.trading_cost_pct)}
      />

      <div className="grid grid-wide">
        <div className="card">
          <div className="card-title">30-day trend &amp; forecast (18k, daily)</div>
          {history.loading ? (
            <Loading label="Loading history…" />
          ) : history.error ? (
            <ErrorMessage message={history.error} onRetry={history.reload} />
          ) : chartData.length >= 2 ? (
            <>
              <PriceChart data={chartData} format={(v) => formatToman(v, unit)} height={260} />
              <div className="chart-legend">
                <span className="chart-legend-item">
                  <span className="legend-swatch legend-actual" aria-hidden="true" /> history
                </span>
                <span className="chart-legend-item">
                  <span className="legend-swatch legend-forecast" aria-hidden="true" /> forecast (band
                  = 90% interval)
                </span>
              </div>
            </>
          ) : (
            <EmptyState title="Not enough history" hint="Price history will appear as data is collected." />
          )}
        </div>

        <div className="card">
          <div className="card-title">Data providers</div>
          <div className="kv">
            <span className="muted">Last update</span>
            <span className="mono">{formatDateTime(s?.last_update, calendar)} (Tehran)</span>
          </div>
          <ProviderStatus providers={s?.providers ?? []} />
        </div>
      </div>

      <div className="grid grid-wide">
        <div className="card">
          <div className="card-title">Gold funds — Tehran exchange (Ayar &amp; peers)</div>
          <GoldFundsPanel state={funds} />
        </div>
      </div>

      <div className="grid grid-wide">
        <ProviderGapCard />
      </div>
    </div>
  )
}
