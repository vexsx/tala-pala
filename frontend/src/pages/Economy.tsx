import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useApi } from '../hooks/useApi'
import { useSettings } from '../lib/settings'
import { ChartTip } from '../components/PriceChart'
import Loading from '../components/Loading'
import Provenance from '../components/Provenance'
import { formatCompact, formatDate, formatGrouped } from '../lib/format'
import type {
  EconomicObservation,
  EconomicObservationsResponse,
  EconomicSeries,
  EconomicSeriesListResponse
} from '../api/types'

/**
 * The economic series this platform stores, which until now had no interface
 * of any kind: twenty-four years of Iranian CPI at monthly resolution, the
 * World Bank's annual mirror back to 1960, and the IMF's WEO for seven
 * countries, all reachable only by curl.
 *
 * THE ONE THING THIS PAGE MUST NOT DO is let a projection read as a
 * measurement. The IMF's WEO carries rows out to 2031; they are FORECASTS. The
 * API withholds them unless asked, and a client that asks takes on the duty of
 * drawing them differently — so here they are a separate, dashed series that
 * starts where the measured one stops, never a continuation of the same
 * stroke, and the legend says which is which. An earlier bug in this codebase
 * turned an IMF 2026 forecast into "measured history" on a calendar rollover;
 * this is the display half of not doing that again.
 *
 * TWO SERIES ARE NEVER PLOTTED TOGETHER. Every index here has its own
 * base_period — SCI's 1400=100 is not the World Bank's 2010=100 — so two lines
 * on one axis would invite a level comparison that means nothing. One series at
 * a time, with its base printed beside it.
 */

/** Recharts needs one row per x value, with measured and projected separated. */
export function toSeriesPoints(obs: EconomicObservation[]): Array<{
  period: string
  measured: number | null
  projected: number | null
}> {
  // Oldest first for a left-to-right chart; the API returns newest first.
  const rows = [...obs].sort((a, b) => a.ref_period_start.localeCompare(b.ref_period_start))
  const firstProjection = rows.findIndex((o) => o.is_projection)
  return rows.map((o, i) => ({
    period: o.ref_period_start,
    measured: o.is_projection ? null : o.value,
    // The last measured point is repeated into the projected series so the
    // dashed line starts where the solid one ends instead of floating a gap.
    // It is the SAME value at the SAME period, so nothing is invented — only
    // the join is drawn.
    projected:
      o.is_projection || (firstProjection > 0 && i === firstProjection - 1) ? o.value : null
  }))
}

function Field({ label, value }: { label: string; value: string }) {
  if (!value) return null
  return (
    <div className="ec-field">
      <div className="field-label">{label}</div>
      <div className="mono small">{value}</div>
    </div>
  )
}

export default function Economy() {
  const { calendar } = useSettings()
  const [code, setCode] = useState<string>('SCI_CPI_URBAN')
  const [withProjections, setWithProjections] = useState(false)

  const list = useApi<EconomicSeriesListResponse>('/series')
  const obs = useApi<EconomicObservationsResponse>(
    `/series/${encodeURIComponent(code)}/observations?limit=500&include_projections=${withProjections}`,
    [code, withProjections]
  )

  const series: EconomicSeries[] = useMemo(() => list.data?.items ?? [], [list.data])
  const selected = useMemo(() => series.find((s) => s.code === code), [series, code])
  const points = useMemo(
    () => (obs.data ? toSeriesPoints(obs.data.observations) : []),
    [obs.data]
  )

  const grouped = useMemo(() => {
    const by = new Map<string, EconomicSeries[]>()
    for (const s of series) {
      const k = s.provider_code || 'other'
      by.set(k, [...(by.get(k) ?? []), s])
    }
    return [...by.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [series])

  if (list.loading) return <Loading />

  const d = obs.data

  return (
    <div className="page ec">
      <h2 className="page-title">Economy</h2>
      <p className="muted small ec-preamble">
        The macroeconomic series this deployment stores, each with the provenance that makes its
        numbers usable: who published it, at what frequency, on which base, and how far its
        coverage reaches. Only one series is charted at a time — every index here carries its own
        base period, so two lines on one axis would invite a level comparison that means nothing.
      </p>

      <div className="card">
        <div className="card-title">Series</div>
        <div className="ec-picker">
          {grouped.map(([provider, items]) => (
            <div key={provider} className="ec-group">
              <div className="field-label">{provider}</div>
              <div className="chip-row wrap">
                {items.map((s) => (
                  <button
                    key={s.code}
                    className={`chip ${code === s.code ? 'active' : ''}`}
                    aria-pressed={code === s.code}
                    onClick={() => setCode(s.code)}
                    title={s.name_en}
                  >
                    {s.code}
                    {s.coverage.projection_count > 0 ? (
                      <span className="ec-proj-dot" title="This series carries projections">
                        {' '}
                        ◌
                      </span>
                    ) : null}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>

      {obs.loading ? <Loading /> : null}
      {obs.error ? (
        <div className="card error-box">
          <div className="card-title">This series could not be read</div>
          <p className="muted small">{obs.error}</p>
        </div>
      ) : null}

      {d ? (
        <>
          <div className="card">
            <div className="row space-between wrap">
              <div className="card-title">
                {selected?.name_en || d.code}
                {selected?.name_fa ? (
                  <span className="bidi-fa muted small" lang="fa" dir="rtl">
                    {' '}
                    {selected.name_fa}
                  </span>
                ) : null}
              </div>
              <div className="muted small">
                {d.measure} · {d.unit} · base {d.base_period || '—'}
              </div>
            </div>

            {/*
             * The projections control. Default OFF, matching the API, because
             * the honest default for a chart of history is history.
             */}
            <div className="row wrap ec-controls">
              <button
                className={`chip ${withProjections ? 'active' : ''}`}
                aria-pressed={withProjections}
                onClick={() => setWithProjections((v) => !v)}
                disabled={d.projections_available === 0}
                data-testid="ec-projections-toggle"
              >
                {withProjections ? 'Hide projections' : 'Show projections'}
              </button>
              <span className="muted small" data-testid="ec-projection-state">
                Currently showing{' '}
                <strong>{withProjections ? 'history and forecasts' : 'measured history only'}</strong>.
              </span>
              <span className="muted small" data-testid="ec-projection-count">
                {d.projections_available === 0
                  ? 'This series carries no projections.'
                  : `${formatGrouped(d.projections_available)} projected period${
                      d.projections_available === 1 ? '' : 's'
                    } stored${
                      withProjections
                        ? `, ${formatGrouped(d.projections_in_page)} shown as a dashed line`
                        : ' and withheld — a forecast is not a measurement'
                    }.`}
              </span>
            </div>

            <div className="ec-chart">
              <ResponsiveContainer width="100%" height={340}>
                <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="period" tick={{ fontSize: 11 }} minTickGap={48} />
                  <YAxis
                    tick={{ fontSize: 11 }}
                    width={64}
                    domain={['auto', 'auto']}
                    tickFormatter={(v: number) => formatCompact(v)}
                  />
                  <Tooltip content={<ChartTip />} />
                  <Line
                    type="monotone"
                    dataKey="measured"
                    name="Measured"
                    stroke="var(--accent)"
                    strokeWidth={2}
                    dot={false}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                  {/*
                   * A SEPARATE series with a DASHED stroke, never a
                   * continuation of the measured one. An IMF WEO row for 2031
                   * is a forecast, and one unbroken line from 2002 to 2031
                   * would present it as a fact.
                   */}
                  <Line
                    type="monotone"
                    dataKey="projected"
                    name="Projected (forecast)"
                    stroke="var(--muted)"
                    strokeDasharray="5 4"
                    strokeWidth={2}
                    dot={false}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                </ComposedChart>
              </ResponsiveContainer>
            </div>

            <div className="ec-legend muted small">
              <span className="ec-key ec-key-measured" /> Measured
              {d.include_projections && d.projections_in_page > 0 ? (
                <>
                  <span className="ec-key ec-key-projected" /> Projected — a forecast, drawn
                  dashed and never joined into the measured stroke
                </>
              ) : null}
            </div>
          </div>

          <div className="card">
            <div className="card-title">Where these numbers come from</div>
            {d.notes ? <p className="muted small ec-notes">{d.notes}</p> : null}
            <div className="ec-fields">
              <Field label="Publisher" value={d.provider_code} />
              <Field label="Measure" value={d.measure} />
              <Field label="Frequency" value={d.frequency} />
              <Field label="Calendar" value={d.calendar} />
              <Field label="Base period" value={d.base_period} />
              <Field label="Splice policy" value={d.splice_policy} />
              <Field
                label="Coverage"
                value={
                  selected?.coverage.first_period && selected?.coverage.last_period
                    ? `${formatDate(selected.coverage.first_period, calendar)} → ${formatDate(
                        selected.coverage.last_period,
                        calendar
                      )}`
                    : '—'
                }
              />
              <Field label="Periods in view" value={formatGrouped(d.count)} />
              <Field
                label="Vintages present"
                value={d.vintages_used.length ? d.vintages_used.join(', ') : '—'}
              />
              <Field
                label="Revisable"
                value={selected ? (selected.revisable ? 'yes' : 'no') : ''}
              />
            </div>

            <div className="ec-tier">
              <Provenance tier={d.quality_tier} isProxy={false} />
            </div>

            {/*
             * The point-in-time disclosure. Every read here is "what was
             * knowable at as_of", and a series with more than one vintage has
             * been revised — which means an older read of the same period
             * would have returned a different number.
             */}
            <p className="muted small ec-pit" data-testid="ec-pit">
              Read as of {formatDate(d.as_of, calendar)}
              {d.as_of_provided ? ' (you asked for that cutoff)' : ' (now)'}: each period shows the
              newest revision that was already <em>available</em> by then, never one published
              afterwards.{' '}
              {d.vintages_used.length > 1
                ? `This history is partly revised — vintages ${d.vintages_used.join(
                    ', '
                  )} are present, so some periods have been restated since first publication.`
                : 'Every period here is a first print; nothing has been revised.'}
            </p>

            {d.has_more ? (
              <p className="muted small">
                Older periods exist beyond this page ({formatGrouped(d.count)} shown).
              </p>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  )
}
