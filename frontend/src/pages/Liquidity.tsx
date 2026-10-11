import { useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useApi } from '../hooks/useApi'
import { useSettings } from '../lib/settings'
import { ChartTip, type ChartTipItem } from '../components/PriceChart'
import Loading from '../components/Loading'
import {
  currencyLabel,
  formatCompactToman,
  formatDate,
  formatGrouped,
  formatPct,
  formatToman,
  type CalendarMode,
  type DisplayUnit
} from '../lib/format'
import {
  CODE_BASE,
  CODE_M2,
  annualAverageReason,
  annualGrowthCrossesBreak,
  annualGrowthReason,
  annualGrowthRows,
  annualLevelRows,
  annualSortValue,
  backtestHorizons,
  backtestModels,
  backtestStatements,
  bandEdges,
  bandTarget,
  bandUndercovers,
  bestModel,
  breaksFor,
  coverageSentence,
  esfandTicks,
  dollarRows,
  findSeries,
  formatMoney,
  formatMoneyNumber,
  hasMoneyData,
  indexBasePeriod,
  indexExtremes,
  indexRows,
  isIsolated,
  jalaliDatePeriod,
  lineKeys,
  longHorizonNote,
  modelLabel,
  moneyIsStale,
  moneyRows,
  moneySortValue,
  moneyTableRows,
  moneyUnit,
  multiplierRows,
  newestOrdinal,
  ordinalPeriod,
  periodLabel,
  periodLabelFa,
  periodOrdinal,
  periodTickLabel,
  periodTicks,
  scenarioKey,
  scenarioStyle,
  sortNullsLast,
  tehranDaysBetween,
  toDisplayMoney,
  windowStart,
  MONEY_STALE_AFTER_DAYS,
  type AnnualMeasure,
  type AnnualSortKey,
  type MoneySortKey,
  type MoneyTableRow,
  type SeriesRow
} from '../lib/liquidity'
import type {
  LiquidityAggregatesResponse,
  LiquidityAnnualRow,
  LiquidityDollarResponse,
  LiquidityPoint
} from '../api/types'

/**
 * Money & liquidity: the Central Bank of Iran's base money (پایه پولی) and
 * liquidity (نقدینگی), month by month and year by year, and the free-market
 * dollar set beside them — with paths showing where the dollar would stand if
 * it kept pace with liquidity growth.
 *
 * WHAT THIS PAGE WILL NOT LET A READER MISTAKE
 *
 *  1. A SCENARIO FOR A FORECAST. The research behind this page measured it:
 *     no money-based model forecast the dollar better than the dollar's own
 *     past trend. So the paths are drawn dashed, labelled as scenarios, start
 *     where the measured price ends and never share its stroke, and the
 *     backtest card says what they are worth in sentences built from the
 *     API's own numbers — never from a sentence typed here.
 *  2. A NOMINAL BAND FOR A MEASURED ONE. The band is labelled only with how
 *     often a band built this way held the real price in the backtest (about
 *     84% where its quantiles promise 90), and the 24-month view says beside
 *     its own button that it held less often still.
 *  3. TWO METHODS FOR ONE SERIES. CBI redefined base money from 1399-12. A
 *     figure on the old method and one on the revised method are different
 *     quantities, so no line joins them, the change is marked on every chart,
 *     and no growth is drawn across it — the API leaves it null and says why.
 *  4. AN OLD MONTH FOR TODAY'S. CBI publishes months late and in batches; the
 *     newest month, its workbook's date and its age are the first line, and a
 *     notice says so when it is older than CBI has ever taken.
 *  5. RIAL FOR TOMAN. CBI's unit is هزار میلیارد ریال; the page shows همت with
 *     the app on toman and the published unit on rial, and prints which.
 *
 * Nothing here is measured in the browser: every level, growth, path,
 * quantile, error and p-value arrives from the API. The page divides by ten
 * to show همت, multiplies a path by the band's own quantiles to draw its
 * edges, and turns the backtest's numbers into sentences — display, all of it.
 */

type Freq = 'monthly' | 'annual'

const MONEY_WINDOWS = [
  { key: '5y', years: 5, label: '5 years' },
  { key: '10y', years: 10, label: '10 years' },
  { key: 'all', years: null, label: 'All' }
] as const
type MoneyWindow = (typeof MONEY_WINDOWS)[number]['key']

const DOLLAR_WINDOWS = [
  { key: '3y', years: 3, label: '3 years' },
  { key: 'all', years: null, label: 'Since 1390' }
] as const
type DollarWindow = (typeof DOLLAR_WINDOWS)[number]['key']

const HORIZONS = [12, 24] as const
type Horizon = (typeof HORIZONS)[number]

/** The months the scenario table reads a path at. */
const TABLE_HORIZONS = [3, 6, 12, 24]

function Fa({ children }: { children: string }) {
  return (
    <span className="bidi-fa" lang="fa" dir="rtl">
      {children}
    </span>
  )
}

/** A growth rate as a plain figure: money growth is not good or bad news, so it wears no colour. */
function Growth({ value, reason }: { value: number | null | undefined; reason?: string }) {
  return (
    <span className="mono" title={value == null ? reason : undefined}>
      {formatPct(value, { digits: 1 })}
    </span>
  )
}

function Chips<K extends string | number>({
  label,
  options,
  value,
  onChange,
  testId
}: {
  label: string
  options: ReadonlyArray<{ key: K; label: string }>
  value: K
  onChange: (k: K) => void
  testId?: string
}) {
  return (
    <div className="chip-row" role="group" aria-label={label} data-testid={testId}>
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          className={`chip ${value === o.key ? 'active' : ''}`}
          aria-pressed={value === o.key}
          onClick={() => onChange(o.key)}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/** A legend key that mirrors its mark: a line (solid or dashed), or a band's wash. */
function Key({ color, dash, area }: { color: string; dash?: boolean; area?: boolean }) {
  return (
    <span
      className={`bx-key ${dash ? 'lq-key-dash' : ''} ${area ? 'lq-key-area' : ''}`}
      style={{ borderTopColor: color, color }}
      aria-hidden="true"
    />
  )
}

// --- the month axis --------------------------------------------------------------------

/**
 * The x axis every month chart shares: Jalali months as integers, ticks on
 * real boundaries. Esfand-only rows (the annual view) are ticked at their own
 * Esfands and named by their year, so "1404" sits under Esfand 1404 rather
 * than under the Farvardin eleven months before it.
 */
function monthAxis(rows: SeriesRow[], esfand = false) {
  const min = rows.length ? rows[0].x : 0
  const max = rows.length ? rows[rows.length - 1].x : 0
  const span = max - min
  return (
    <XAxis
      dataKey="x"
      type="number"
      domain={[min, max]}
      ticks={esfand ? esfandTicks(rows.map((r) => r.x)) : periodTicks(min, max)}
      tickFormatter={(v: number) => (esfand ? String(Math.floor(v / 12)) : periodTickLabel(v, span))}
      tick={{ fontSize: 11 }}
      allowDecimals={false}
    />
  )
}

/**
 * The shared tooltip, with the month written out ('Esfand 1404') and, with
 * the Gregorian calendar on, the day that month ended. A line with no value
 * at the month is left out rather than listed as a dash.
 */
function periodTip(format: (v: number) => string, calendar: CalendarMode, ends?: Map<string, string>) {
  return function PeriodTip(props: { active?: boolean; payload?: unknown; label?: string | number }) {
    const x = Number(props.label)
    const period = Number.isFinite(x) ? ordinalPeriod(x) : ''
    const end = ends?.get(period)
    const label = period
      ? `${periodLabel(period)}${calendar === 'gregorian' && end ? ` (ended ${end})` : ''}`
      : undefined
    const payload = ((props.payload as ChartTipItem[] | undefined) ?? []).filter(
      (i) => i.value !== null && i.value !== undefined
    )
    return <ChartTip active={props.active} payload={payload} label={label} format={format} />
  }
}

/**
 * A point with no neighbour on its own line is drawn as a dot: a line needs
 * two points, and base money's lone revised month would otherwise vanish.
 */
function isoDot(rows: SeriesRow[], key: string, color: string) {
  return function IsoDot(props: { cx?: number; cy?: number; index?: number; key?: string }) {
    if (props.index === undefined || props.cx == null || props.cy == null || !isIsolated(rows, props.index, key)) {
      return <g key={props.key} />
    }
    return <circle key={props.key} cx={props.cx} cy={props.cy} r={3.5} fill={color} stroke="var(--panel)" strokeWidth={2} />
  }
}

/** Base money's method changes, dashed, on a month chart. */
function breakLines(resp: LiquidityAggregatesResponse, rows: SeriesRow[]) {
  if (rows.length === 0) return null
  const min = rows[0].x
  const max = rows[rows.length - 1].x
  return breaksFor(resp.breaks, CODE_BASE)
    .filter((b) => b.x >= min && b.x <= max)
    .map((b) => (
      <ReferenceLine
        key={`${b.code}-${b.from}`}
        x={b.x}
        stroke="var(--muted)"
        strokeDasharray="2 3"
        strokeOpacity={0.9}
        ifOverflow="hidden"
      />
    ))
}

/**
 * Base money's lines, one per CBI method, all in its one colour. A function,
 * not a component: Recharts draws only the Line elements it finds among a
 * chart's direct children.
 */
function baseLines(rows: SeriesRow[], prefix: string, name: string) {
  return lineKeys(rows, prefix).map((k) => {
    const method = k.slice(prefix.length + 1)
    return (
      <Line
        key={k}
        type="linear"
        dataKey={k}
        name={method === 'unmarked' ? name : `${name} (${method} method)`}
        stroke="var(--series-2)"
        strokeWidth={2}
        dot={isoDot(rows, k, 'var(--series-2)')}
        connectNulls={false}
        isAnimationActive={false}
      />
    )
  })
}

function MoneyLegend({ hasBreak }: { hasBreak: boolean }) {
  return (
    <div className="bx-legend" data-testid="lq-money-legend">
      <span className="bx-legend-item">
        <Key color="var(--series-1)" /> Liquidity (M2) <Fa>نقدینگی</Fa>
      </span>
      <span className="bx-legend-item">
        <Key color="var(--series-2)" /> Base money <Fa>پایه پولی</Fa>
      </span>
      {hasBreak ? (
        <span className="bx-legend-item">
          <span className="lq-key-break" aria-hidden="true" /> base-money method change — the line breaks there
        </span>
      ) : null}
    </div>
  )
}

// --- the header ----------------------------------------------------------------------------

function AsOf({ resp, calendar }: { resp: LiquidityAggregatesResponse; calendar: CalendarMode }) {
  const latest = resp.latest
  if (!latest) return null
  const point = findSeries(resp, CODE_M2)?.points.find((p) => p.period === latest.period)
  const lag = point && latest.published_at ? tehranDaysBetween(point.gregorian_end, latest.published_at) : null
  return (
    <p className="muted small lq-asof" data-testid="lq-as-of">
      Newest CBI month: <strong>{periodLabel(latest.period)}</strong> (<Fa>{periodLabelFa(latest.period)}</Fa>
      {point ? `, ended ${formatDate(point.gregorian_end, calendar)}` : ''}).{' '}
      {latest.published_at ? (
        <>
          Its workbook was saved {formatDate(latest.published_at, calendar)}
          {lag !== null ? `, ${formatGrouped(lag)} days after the month ended` : ''} — CBI states no release date, so
          that is the nearest there is.{' '}
        </>
      ) : (
        <>CBI states no release date for it. </>
      )}
      First readable here {formatDate(latest.available_at, calendar)}. On {formatDate(resp.as_of, calendar)} the
      newest month is {formatGrouped(latest.age_days)} days old.{' '}
      {latest.source_url ? (
        <a href={latest.source_url} target="_blank" rel="noopener noreferrer">
          CBI’s workbook
        </a>
      ) : null}
    </p>
  )
}

function MoneyAgeNotice({ resp }: { resp: LiquidityAggregatesResponse }) {
  const latest = resp.latest
  if (!latest || !moneyIsStale(latest, resp.data_age)) return null
  return (
    <div className="callout callout-warn" data-testid="lq-stale" role="status">
      <div>
        Nothing newer than <strong>{periodLabel(latest.period)}</strong> is stored: the newest money figures are{' '}
        {formatGrouped(latest.age_days)} days old.
      </div>
      <div className="small">
        CBI’s monthly workbooks have arrived 10 to {MONEY_STALE_AFTER_DAYS} days after the month they describe, in
        batches, so either CBI has published nothing newer or it has not been read yet. Every money figure below — and
        every scenario rate built on them — describes {periodLabel(latest.period)}, not today.
        {resp.data_age?.warning ? ` ${resp.data_age.warning}` : ''}
      </div>
    </div>
  )
}

/** The newest month's two stocks and their multiplier, as stat tiles. */
function Headline({ resp, unit }: { resp: LiquidityAggregatesResponse; unit: DisplayUnit }) {
  const period = resp.latest?.period
  if (!period) return null
  const at = (code: string) => findSeries(resp, code)?.points.find((p) => p.period === period)
  const mult = resp.multiplier.find((m) => m.period === period)
  const tile = (label: string, fa: string, p: LiquidityPoint | undefined, testId: string) => (
    <div className="card bx-tile" data-testid={testId}>
      <div className="field-label">
        {label} <Fa>{fa}</Fa>
      </div>
      <div className="bx-tile-value">{p ? formatMoney(p.value, unit) : '—'}</div>
      {p ? (
        <div className="muted small">
          {formatPct(p.yoy_pct, { digits: 1 })} on a year before · {formatPct(p.mom_pct, { digits: 1 })} on the month
          before{p.method ? ` · ${p.method} method` : ''}
        </div>
      ) : null}
    </div>
  )
  return (
    <div className="bx-tiles" data-testid="lq-headline">
      {tile('Liquidity (M2)', 'نقدینگی', at(CODE_M2), 'lq-tile-m2')}
      {tile('Base money', 'پایه پولی', at(CODE_BASE), 'lq-tile-base')}
      <div className="card bx-tile" data-testid="lq-tile-multiplier">
        <div className="field-label">Multiplier</div>
        <div className="bx-tile-value">{mult ? formatGrouped(mult.value, 3) : '—'}</div>
        <div className="muted small">liquidity ÷ base money, {periodLabel(period)}</div>
      </div>
    </div>
  )
}

/** Before the first CBI ingest. The API's own notes are in the notes card, once. */
function NotIngested() {
  return (
    <div className="card callout callout-warn" data-testid="lq-not-ingested" role="status">
      <div className="card-title">The CBI money series are not stored yet</div>
      <p className="bx-verdict">
        This page reads liquidity (<Fa>نقدینگی</Fa>, <code>{CODE_M2}</code>) and base money (<Fa>پایه پولی</Fa>,{' '}
        <code>{CODE_BASE}</code>) from the Central Bank’s monthly workbooks. No observation of either is stored in this
        deployment yet, so there is nothing to chart, and the scenario paths below — paced by liquidity growth — cannot be
        drawn either.
      </p>
    </div>
  )
}

// --- the money section -------------------------------------------------------------------------

function LevelsCard({
  resp,
  freq,
  fromX,
  unit,
  calendar,
  ends
}: {
  resp: LiquidityAggregatesResponse
  freq: Freq
  fromX: number | undefined
  unit: DisplayUnit
  calendar: CalendarMode
  ends: Map<string, string>
}) {
  const [log, setLog] = useState(false)
  const rows = useMemo(
    () =>
      freq === 'annual'
        ? annualLevelRows(resp, unit, fromX === undefined ? undefined : Math.floor(fromX / 12))
        : moneyRows(resp, (p) => toDisplayMoney(p.value, unit), { fromX }),
    [resp, freq, fromX, unit]
  )
  const mu = moneyUnit(unit)
  const quarterly = freq === 'monthly' && (findSeries(resp, CODE_M2)?.points ?? []).some(
    (p) => p.frequency === 'Q' && (fromX === undefined || (periodOrdinal(p.period) ?? 0) >= fromX)
  )
  return (
    <div className="card" data-testid="lq-levels">
      <div className="row space-between wrap bx-card-head">
        <div className="card-title">Levels — {freq === 'annual' ? 'at each Esfand' : 'at each month end'}</div>
        <button
          type="button"
          className={`chip ${log ? 'active' : ''}`}
          aria-pressed={log}
          onClick={() => setLog(!log)}
          title="A log scale draws equal percentage growth as equal heights"
          data-testid="lq-log-toggle"
        >
          Log scale
        </button>
      </div>
      <p className="muted small bx-unit" data-testid="lq-levels-unit">
        In {mu.long}. Both are stocks in the same unit, so they share one axis
        {log ? '; on a log scale equal heights are equal percentage growth' : ''}.
        {quarterly ? ' Before Tir 1397 CBI’s history is quarter-ends, joined quarter to quarter.' : ''}
      </p>
      <MoneyLegend hasBreak={breaksFor(resp.breaks, CODE_BASE).length > 0} />
      <figure className="lq-figure" aria-label={`Liquidity and base money levels in ${mu.label}`}>
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
            <CartesianGrid stroke="var(--border)" vertical={false} />
            {monthAxis(rows, freq === 'annual')}
            <YAxis
              tick={{ fontSize: 11 }}
              width={64}
              scale={log ? 'log' : 'auto'}
              domain={log ? ['auto', 'auto'] : [0, 'auto']}
              allowDataOverflow={log}
              tickFormatter={(v: number) => formatMoneyNumber(v)}
            />
            <Tooltip content={periodTip((v) => `${formatMoneyNumber(v)} ${mu.label}`, calendar, ends)} />
            {breakLines(resp, rows)}
            <Line
              type="linear"
              dataKey="m2"
              name="Liquidity (M2)"
              stroke="var(--series-1)"
              strokeWidth={2}
              dot={isoDot(rows, 'm2', 'var(--series-1)')}
              connectNulls={false}
              isAnimationActive={false}
            />
            {baseLines(rows, 'base', 'Base money')}
          </LineChart>
        </ResponsiveContainer>
      </figure>
    </div>
  )
}

function GrowthCard({
  resp,
  freq,
  fromX,
  calendar,
  ends
}: {
  resp: LiquidityAggregatesResponse
  freq: Freq
  fromX: number | undefined
  calendar: CalendarMode
  ends: Map<string, string>
}) {
  const [measure, setMeasure] = useState<AnnualMeasure>('end')
  const monthly = useMemo(() => moneyRows(resp, (p) => p.yoy_pct, { fromX }), [resp, fromX])
  const fromYear = fromX === undefined ? undefined : Math.floor(fromX / 12)
  const annualRows = useMemo(
    () => annualGrowthRows(resp.annual ?? [], measure, fromYear),
    [resp.annual, measure, fromYear]
  )
  const missingBase = (resp.annual ?? []).filter(
    (a) => (fromYear === undefined || a.year >= fromYear) && annualGrowthCrossesBreak(resp, a, CODE_BASE, measure)
  )
  return (
    <div className="card" data-testid="lq-growth">
      <div className="row space-between wrap bx-card-head">
        <div className="card-title">
          Growth — {freq === 'annual' ? (measure === 'end' ? 'Esfand over Esfand' : 'yearly average over yearly average') : 'year on year, each month'}
        </div>
        {freq === 'annual' ? (
          <Chips<AnnualMeasure>
            label="Annual growth measure"
            options={[
              { key: 'end', label: 'Esfand → Esfand' },
              { key: 'avg', label: 'Yearly average' }
            ]}
            value={measure}
            onChange={setMeasure}
            testId="lq-measure"
          />
        ) : null}
      </div>
      {freq === 'monthly' ? (
        <>
          <p className="muted small bx-unit">
            Each month’s stock against the same month a year before, as the API measured it. Base money’s line stops
            where CBI changed its method: a growth across the change compares two different definitions, so none is
            drawn.
          </p>
          <MoneyLegend hasBreak={breaksFor(resp.breaks, CODE_BASE).length > 0} />
          <figure className="lq-figure" aria-label="Year-on-year growth of liquidity and base money">
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={monthly} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
                <CartesianGrid stroke="var(--border)" vertical={false} />
                {monthAxis(monthly)}
                <YAxis tick={{ fontSize: 11 }} width={48} tickFormatter={(v: number) => `${v}%`} />
                <Tooltip content={periodTip((v) => formatPct(v, { digits: 1 }), calendar, ends)} />
                <ReferenceLine y={0} stroke="var(--muted)" strokeOpacity={0.6} />
                {breakLines(resp, monthly)}
                <Line
                  type="linear"
                  dataKey="m2"
                  name="Liquidity (M2)"
                  stroke="var(--series-1)"
                  strokeWidth={2}
                  dot={isoDot(monthly, 'm2', 'var(--series-1)')}
                  connectNulls={false}
                  isAnimationActive={false}
                />
                {baseLines(monthly, 'base', 'Base money')}
              </LineChart>
            </ResponsiveContainer>
          </figure>
        </>
      ) : (
        <>
          <p className="muted small bx-unit">
            {measure === 'end'
              ? 'The Esfand stock against the Esfand before: CBI’s own headline growth.'
              : 'The mean of the twelve month-end stocks against the mean of the year before — only for years with every month stored.'}
          </p>
          <div className="bx-legend">
            <span className="bx-legend-item">
              <span className="mf-key" style={{ background: 'var(--series-1)' }} aria-hidden="true" /> Liquidity (M2)
            </span>
            <span className="bx-legend-item">
              <span className="mf-key" style={{ background: 'var(--series-2)' }} aria-hidden="true" /> Base money
            </span>
          </div>
          <figure className="lq-figure" aria-label="Annual growth of liquidity and base money">
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={annualRows} margin={{ top: 8, right: 16, bottom: 0, left: 8 }} barGap={2}>
                <CartesianGrid stroke="var(--border)" vertical={false} />
                <XAxis dataKey="year" tick={{ fontSize: 11 }} minTickGap={16} />
                <YAxis tick={{ fontSize: 11 }} width={48} tickFormatter={(v: number) => `${v}%`} />
                <Tooltip content={<ChartTip format={(v) => formatPct(v, { digits: 1 })} />} cursor={{ fill: 'var(--panel-2)' }} />
                <ReferenceLine y={0} stroke="var(--muted)" strokeOpacity={0.6} />
                <Bar dataKey="m2" name="Liquidity (M2)" fill="var(--series-1)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
                <Bar dataKey="base" name="Base money" fill="var(--series-2)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </figure>
          {missingBase.length > 0 ? (
            <p className="muted small" data-testid="lq-growth-missing">
              No base-money bar for {missingBase.map((m) => m.year).join(', ')}: CBI changed its base-money method inside
              the months that growth compares, and it is not measured across the change.
            </p>
          ) : null}
        </>
      )}
    </div>
  )
}

function MultiplierCard({
  resp,
  freq,
  fromX,
  calendar,
  ends
}: {
  resp: LiquidityAggregatesResponse
  freq: Freq
  fromX: number | undefined
  calendar: CalendarMode
  ends: Map<string, string>
}) {
  const rows = useMemo(
    () => multiplierRows(resp, { fromX, esfandOnly: freq === 'annual' }),
    [resp, fromX, freq]
  )
  return (
    <div className="card" data-testid="lq-multiplier">
      <div className="card-title">Money multiplier — liquidity ÷ base money</div>
      <p className="muted small bx-unit">
        How many units of liquidity stand on each unit of base money, {freq === 'annual' ? 'at each Esfand' : 'month by month'}.
        It divides by base money, so it breaks where base money’s method changed.
      </p>
      <figure className="lq-figure" aria-label="Money multiplier">
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
            <CartesianGrid stroke="var(--border)" vertical={false} />
            {monthAxis(rows, freq === 'annual')}
            <YAxis tick={{ fontSize: 11 }} width={40} domain={['auto', 'auto']} tickFormatter={(v: number) => v.toFixed(1)} />
            <Tooltip content={periodTip((v) => formatGrouped(v, 3), calendar, ends)} />
            {breakLines(resp, rows)}
            {lineKeys(rows, 'mult').map((k) => (
              <Line
                key={k}
                type="linear"
                dataKey={k}
                name="Multiplier"
                stroke="var(--series-1)"
                strokeWidth={2}
                dot={isoDot(rows, k, 'var(--series-1)')}
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </figure>
    </div>
  )
}

// --- the data tables ------------------------------------------------------------------------------

/**
 * A sortable heading: a button inside the cell, as on the Stocks screen, so
 * the sort is reachable from the keyboard and announced as a control.
 */
function SortHeader<K extends string>({
  k,
  label,
  sort,
  setSort,
  title,
  num = true,
  rowSpan
}: {
  k: K
  label: string
  sort: { key: K; desc: boolean }
  setSort: (s: { key: K; desc: boolean }) => void
  title?: string
  num?: boolean
  rowSpan?: number
}) {
  return (
    <th
      scope="col"
      rowSpan={rowSpan}
      className={num ? 'num' : undefined}
      aria-sort={sort.key === k ? (sort.desc ? 'descending' : 'ascending') : 'none'}
    >
      <button
        type="button"
        className="th-sort lq-sort"
        title={title}
        onClick={() => setSort({ key: k, desc: sort.key === k ? !sort.desc : true })}
      >
        {label}
        {sort.key === k ? (
          <span className="th-sort-mark" aria-hidden="true">
            {sort.desc ? '↓' : '↑'}
          </span>
        ) : null}
      </button>
    </th>
  )
}

function growthReason(p: LiquidityPoint | null, field: 'mom_pct' | 'yoy_pct' | 'ytd_pct'): string | undefined {
  return p?.null_reason?.[field]
}

/** Rows a table shows before "show all": two years of months, so the charts below stay in reach. */
const TABLE_ROWS = 24

function MoreRows({
  total,
  expanded,
  onToggle,
  noun
}: {
  total: number
  expanded: boolean
  onToggle: () => void
  noun: string
}) {
  if (total <= TABLE_ROWS) return null
  return (
    <div className="row wrap lq-more">
      <span className="muted small">
        {expanded ? `All ${formatGrouped(total)} ${noun}` : `The first ${TABLE_ROWS} of ${formatGrouped(total)} ${noun}`} in
        the order chosen above.
      </span>
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        aria-expanded={expanded}
        onClick={onToggle}
        data-testid="lq-more"
      >
        {expanded ? `Show ${TABLE_ROWS}` : `Show all ${formatGrouped(total)}`}
      </button>
    </div>
  )
}

function MonthlyTable({
  resp,
  fromX,
  unit,
  calendar
}: {
  resp: LiquidityAggregatesResponse
  fromX: number | undefined
  unit: DisplayUnit
  calendar: CalendarMode
}) {
  const [sort, setSort] = useState<{ key: MoneySortKey; desc: boolean }>({ key: 'period', desc: true })
  const [expanded, setExpanded] = useState(false)
  const all = useMemo(() => moneyTableRows(resp, fromX), [resp, fromX])
  const sorted = useMemo(() => sortNullsLast(all, (r) => moneySortValue(r, sort.key), sort.desc), [all, sort])
  const rows = expanded ? sorted : sorted.slice(0, TABLE_ROWS)
  const breakAt = useMemo(() => new Set(breaksFor(resp.breaks, CODE_BASE).map((b) => b.from)), [resp.breaks])
  const mu = moneyUnit(unit)
  const level = (p: LiquidityPoint | null) => (p ? formatMoneyNumber(toDisplayMoney(p.value, unit)) : '—')
  const h = (k: MoneySortKey, label: string, title?: string) => (
    <SortHeader<MoneySortKey> k={k} label={label} sort={sort} setSort={setSort} title={title} />
  )
  return (
    <>
      <div className="table-wrap">
        <table className="table bx-table lq-table" data-testid="lq-table">
          <caption className="sr-only">Liquidity and base money by month, in {mu.label}, with growth</caption>
          <thead>
            <tr>
              <SortHeader<MoneySortKey> k="period" label="Month" sort={sort} setSort={setSort} num={false} rowSpan={2} />
              <th scope="colgroup" colSpan={4} className="lq-group">
                Liquidity (M2)
              </th>
              <th scope="colgroup" colSpan={5} className="lq-group">
                Base money
              </th>
              <SortHeader<MoneySortKey>
                k="multiplier"
                label="Multiplier"
                title="Liquidity ÷ base money, same month"
                sort={sort}
                setSort={setSort}
                rowSpan={2}
              />
            </tr>
            <tr>
              {h('m2', mu.label)}
              {h('m2_mom', 'm/m', 'Against the month before')}
              {h('m2_yoy', 'y/y', 'Against the same month a year before')}
              {h('m2_ytd', 'Since Esfand', 'Against the previous Esfand')}
              {h('base', mu.label)}
              {h('base_mom', 'm/m', 'Against the month before')}
              {h('base_yoy', 'y/y', 'Against the same month a year before')}
              {h('base_ytd', 'Since Esfand', 'Against the previous Esfand')}
              <th scope="col">Method</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r: MoneyTableRow) => {
              const p = r.m2 ?? r.base
              return (
                <tr key={r.period} data-testid={`lq-row-${r.period}`}>
                  <td>
                    {periodLabel(r.period)}
                    <div className="muted small">
                      <span className="mono">{r.period}</span>
                      {p?.frequency === 'Q' ? ' · quarter-end' : ''}
                      {calendar === 'gregorian' && p ? ` · ended ${p.gregorian_end}` : ''}
                    </div>
                  </td>
                  <td className="num mono">{level(r.m2)}</td>
                  <td className="num"><Growth value={r.m2?.mom_pct} reason={growthReason(r.m2, 'mom_pct')} /></td>
                  <td className="num"><Growth value={r.m2?.yoy_pct} reason={growthReason(r.m2, 'yoy_pct')} /></td>
                  <td className="num"><Growth value={r.m2?.ytd_pct} reason={growthReason(r.m2, 'ytd_pct')} /></td>
                  <td className="num mono">{level(r.base)}</td>
                  <td className="num"><Growth value={r.base?.mom_pct} reason={growthReason(r.base, 'mom_pct')} /></td>
                  <td className="num"><Growth value={r.base?.yoy_pct} reason={growthReason(r.base, 'yoy_pct')} /></td>
                  <td className="num"><Growth value={r.base?.ytd_pct} reason={growthReason(r.base, 'ytd_pct')} /></td>
                  <td className="small">
                    {r.base?.method ?? '—'}
                    {breakAt.has(r.period) ? (
                      <span className="badge badge-warn bx-badge" data-testid={`lq-break-${r.period}`}>
                        method change
                      </span>
                    ) : null}
                  </td>
                  <td className="num mono">{r.multiplier == null ? '—' : formatGrouped(r.multiplier, 3)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <MoreRows total={sorted.length} expanded={expanded} onToggle={() => setExpanded(!expanded)} noun="months" />
    </>
  )
}

function AnnualTable({
  resp,
  fromYear,
  unit
}: {
  resp: LiquidityAggregatesResponse
  fromYear: number | undefined
  unit: DisplayUnit
}) {
  const [sort, setSort] = useState<{ key: AnnualSortKey; desc: boolean }>({ key: 'year', desc: true })
  const all = useMemo(
    () => (resp.annual ?? []).filter((a) => fromYear === undefined || a.year >= fromYear),
    [resp.annual, fromYear]
  )
  const [expanded, setExpanded] = useState(false)
  const sorted = useMemo(() => sortNullsLast(all, (r) => annualSortValue(r, sort.key), sort.desc), [all, sort])
  const rows = expanded ? sorted : sorted.slice(0, TABLE_ROWS)
  const mu = moneyUnit(unit)
  const level = (v: number | null | undefined) => (v == null ? '—' : formatMoneyNumber(toDisplayMoney(v, unit)))
  const h = (k: AnnualSortKey, label: string, title?: string) => (
    <SortHeader<AnnualSortKey> k={k} label={label} sort={sort} setSort={setSort} title={title} />
  )
  const cells = (a: LiquidityAnnualRow, code: string) => (
    <>
      <td className="num mono">{level(a.end_value[code])}</td>
      <td className="num">
        <Growth value={a.end_growth_pct[code]} reason={annualGrowthReason(resp, a, code, 'end') ?? undefined} />
      </td>
      <td className="num mono" title={annualAverageReason(resp, a, code) ?? undefined}>
        {level(a.avg_value?.[code])}
      </td>
      <td className="num">
        <Growth value={a.avg_growth_pct?.[code]} reason={annualGrowthReason(resp, a, code, 'avg') ?? undefined} />
      </td>
    </>
  )
  return (
    <>
      <div className="table-wrap">
        <table className="table bx-table lq-table" data-testid="lq-table">
          <caption className="sr-only">Liquidity and base money by Jalali year, in {mu.label}, with growth</caption>
          <thead>
            <tr>
              <SortHeader<AnnualSortKey> k="year" label="Year" sort={sort} setSort={setSort} num={false} rowSpan={2} />
              <th scope="colgroup" colSpan={4} className="lq-group">
                Liquidity (M2)
              </th>
              <th scope="colgroup" colSpan={4} className="lq-group">
                Base money
              </th>
              <th scope="col" rowSpan={2} className="num">
                Months stored
              </th>
            </tr>
            <tr>
              {h('m2_end', `Esfand, ${mu.label}`)}
              {h('m2_end_growth', 'Growth', 'Esfand over the previous Esfand')}
              {h('m2_avg', 'Year average', 'Mean of the twelve month-end stocks')}
              {h('m2_avg_growth', 'Growth', 'Yearly average over the previous yearly average')}
              {h('base_end', `Esfand, ${mu.label}`)}
              {h('base_end_growth', 'Growth', 'Esfand over the previous Esfand')}
              {h('base_avg', 'Year average', 'Mean of the twelve month-end stocks')}
              {h('base_avg_growth', 'Growth', 'Yearly average over the previous yearly average')}
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.year} data-testid={`lq-year-${a.year}`}>
                <td>
                  {a.year}
                  {a.partial ? <div className="muted small">partial year</div> : null}
                </td>
                {cells(a, CODE_M2)}
                {cells(a, CODE_BASE)}
                <td className="num mono">{formatGrouped(a.months_present)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <MoreRows total={sorted.length} expanded={expanded} onToggle={() => setExpanded(!expanded)} noun="years" />
    </>
  )
}

// --- the dollar ---------------------------------------------------------------------------------------

function DollarCard({
  d,
  horizon,
  setHorizon,
  loading,
  unit,
  calendar
}: {
  d: LiquidityDollarResponse
  horizon: Horizon
  setHorizon: (h: Horizon) => void
  loading: boolean
  unit: DisplayUnit
  calendar: CalendarMode
}) {
  const [histWindow, setHistWindow] = useState<DollarWindow>('3y')
  const [log, setLog] = useState(false)
  const [picked, setPicked] = useState<string | null>(null)
  const drawn = d.scenarios.filter((s) => s.path.length > 0)
  const bandKey = bandTarget(
    d.band,
    drawn.map((s) => s.key),
    picked
  )
  // A path keeps its number — and its colour — when another cannot be drawn.
  const pathNo = (key: string) => d.scenarios.findIndex((s) => s.key === key) + 1
  const anchorPeriod = d.anchor ? jalaliDatePeriod(d.anchor.jalali) : null
  const anchorX = anchorPeriod ? periodOrdinal(anchorPeriod) : null
  const moneyX = d.money_as_of ? periodOrdinal(d.money_as_of.period) : null
  const years = DOLLAR_WINDOWS.find((w) => w.key === histWindow)?.years ?? null
  const lastHistX = d.history.length ? periodOrdinal(d.history[d.history.length - 1].period) : null
  const fromX = windowStart(lastHistX, years)
  const rows = useMemo(() => dollarRows(d, { fromX, bandKey }), [d, fromX, bandKey])
  const band = d.band
  const bandColor = bandKey ? scenarioStyle(d.scenarios.findIndex((s) => s.key === bandKey)).color : 'var(--muted)'
  const longNote = longHorizonNote(band)
  const fmt = (v: number) => formatToman(v, unit)
  const missing = d.scenarios.filter((s) => s.path.length === 0 || s.annual_growth_pct === null)

  return (
    <div className={`card ${loading ? 'bx-refetch' : ''}`} data-testid="lq-dollar">
      <div className="card-title">The dollar against liquidity-paced paths</div>
      <p className="bx-verdict">
        <Fa>مسیر هم‌پای رشد نقدینگی — سناریو، نه پیش‌بینی</Fa>
        <br />
        Each dashed path shows where the free-market dollar would stand if, from its last close, it grew exactly as fast
        as liquidity did over one stated span. They are scenarios — what keeping pace would mean — not forecasts of
        what it will do.
      </p>
      <div className="row wrap bx-controls">
        <Chips<Horizon>
          label="Months ahead"
          options={HORIZONS.map((h) => ({ key: h, label: `${h} months` }))}
          value={horizon}
          onChange={setHorizon}
          testId="lq-horizon"
        />
        <Chips<DollarWindow>
          label="History shown"
          options={DOLLAR_WINDOWS.map((w) => ({ key: w.key, label: w.label }))}
          value={histWindow}
          onChange={setHistWindow}
        />
        <button
          type="button"
          className={`chip ${log ? 'active' : ''}`}
          aria-pressed={log}
          onClick={() => setLog(!log)}
          title="A log scale draws equal percentage moves as equal heights"
        >
          Log scale
        </button>
      </div>
      {longNote ? (
        <p className={`small ${horizon === 24 ? 'lq-coverage-strong' : 'muted'}`} data-testid="lq-horizon-note">
          24 months: {longNote}
        </p>
      ) : null}
      {band && band.applied_to === 'each' && drawn.length > 1 ? (
        <div className="row wrap bx-controls">
          <span className="field-label">Band around</span>
          <Chips<string>
            label="Scenario the band surrounds"
            options={drawn.map((s) => ({ key: s.key, label: `Path ${pathNo(s.key)}` }))}
            value={bandKey ?? ''}
            onChange={setPicked}
            testId="lq-band-pick"
          />
        </div>
      ) : null}

      <div className="bx-legend" data-testid="lq-dollar-legend">
        <span className="bx-legend-item">
          <Key color="var(--text)" /> Dollar, month-end close
        </span>
        {d.scenarios.map((s, i) =>
          s.path.length > 0 ? (
            <span key={s.key} className="bx-legend-item">
              <Key color={scenarioStyle(i).color} dash /> Path {i + 1}: {s.label_en}
              {s.annual_growth_pct !== null ? ` (${s.annual_growth_pct.toFixed(1)}% a year)` : ''}
            </span>
          ) : null
        )}
        {d.trend && d.trend.path.length > 0 ? (
          <span className="bx-legend-item">
            <Key color="var(--muted)" dash /> The dollar’s own trend since {d.trend.since} (
            {d.trend.annual_growth_pct.toFixed(1)}% a year)
          </span>
        ) : null}
        {band && bandKey ? (
          <span className="bx-legend-item">
            <Key color={bandColor} area /> Band around path {pathNo(bandKey)}
          </span>
        ) : null}
        {anchorX !== null && horizon > 12 ? (
          <span className="bx-legend-item">
            <span className="lq-key-break" aria-hidden="true" /> 12 months after the last close
          </span>
        ) : null}
      </div>

      {rows.length === 0 ? (
        <p className="muted" data-testid="lq-no-history">No dollar history is stored.</p>
      ) : (
        <figure className="lq-figure" aria-label="Dollar history with liquidity-paced scenario paths">
          <ResponsiveContainer width="100%" height={360}>
            <ComposedChart data={rows} margin={{ top: 16, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} />
              {monthAxis(rows)}
              <YAxis
                tick={{ fontSize: 11 }}
                width={64}
                scale={log ? 'log' : 'auto'}
                domain={['auto', 'auto']}
                tickFormatter={(v: number) => formatCompactToman(v, unit)}
              />
              <Tooltip content={periodTip(fmt, calendar)} />
              {band && bandKey ? (
                <Area
                  type="linear"
                  dataKey="band"
                  name={`Band around path ${pathNo(bandKey)}`}
                  fill={bandColor}
                  fillOpacity={0.14}
                  stroke="none"
                  connectNulls={false}
                  isAnimationActive={false}
                />
              ) : null}
              {moneyX !== null && rows.length && moneyX >= rows[0].x ? (
                // Each label on the outer side of its line: the two lines
                // stand months apart and their labels would otherwise meet.
                <ReferenceLine
                  x={moneyX}
                  stroke="var(--muted)"
                  strokeDasharray="2 3"
                  label={{ value: 'money data', position: 'insideTopRight', fill: 'var(--muted)', fontSize: 11 }}
                />
              ) : null}
              {anchorX !== null ? (
                <ReferenceLine
                  x={anchorX}
                  stroke="var(--muted)"
                  label={{ value: 'last close', position: 'insideTopLeft', fill: 'var(--muted)', fontSize: 11 }}
                />
              ) : null}
              {/* Twelve months out: the band's coverage beyond it is stated on its own. */}
              {anchorX !== null && horizon > 12 ? (
                <ReferenceLine x={anchorX + 12} stroke="var(--muted)" strokeDasharray="1 3" strokeOpacity={0.8} />
              ) : null}
              <Line
                type="linear"
                dataKey="actual"
                name="Dollar, month-end close"
                stroke="var(--text)"
                strokeWidth={2}
                dot={false}
                connectNulls={false}
                isAnimationActive={false}
              />
              {d.scenarios.map((s, i) =>
                s.path.length > 0 ? (
                  <Line
                    key={s.key}
                    type="linear"
                    dataKey={scenarioKey(s.key)}
                    name={`Path ${i + 1} (scenario)`}
                    stroke={scenarioStyle(i).color}
                    strokeDasharray={scenarioStyle(i).dash}
                    strokeWidth={2}
                    dot={false}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                ) : null
              )}
              {d.trend && d.trend.path.length > 0 ? (
                <Line
                  type="linear"
                  dataKey="trend"
                  name="Own trend"
                  stroke="var(--muted)"
                  strokeDasharray="4 4"
                  strokeWidth={1.5}
                  dot={false}
                  connectNulls={false}
                  isAnimationActive={false}
                />
              ) : null}
            </ComposedChart>
          </ResponsiveContainer>
        </figure>
      )}

      {band && bandKey ? (
        <p className="small" data-testid="lq-band-label" title={band.note || undefined}>
          <strong>The band</strong> around path {pathNo(bandKey)}: {coverageSentence(band)} Its edges come from
          how far past paths built this way missed
          {bandUndercovers(band) ? '; the real price has left it more often than the way it is built would suggest' : ''}.
        </p>
      ) : drawn.length > 0 ? (
        <p className="muted small" data-testid="lq-no-band">
          The API returned no band for these paths, so none is drawn.
        </p>
      ) : null}

      {d.anchor ? (
        <p className="muted small" data-testid="lq-anchor">
          Every path starts at the last stored close, <strong>{fmt(d.anchor.close_toman)}</strong> on{' '}
          {calendar === 'jalali' ? d.anchor.jalali : d.anchor.date} — {d.anchor.note}.{' '}
          {d.money_as_of ? (
            <>
              Their growth rates are read from {periodLabel(d.money_as_of.period)}, the newest liquidity month stored
              {d.money_as_of.published_at ? ` (workbook saved ${formatDate(d.money_as_of.published_at, calendar)})` : ''}.
            </>
          ) : null}
        </p>
      ) : null}

      {drawn.length === 0 ? (
        <div className="callout callout-warn" data-testid="lq-no-scenarios" role="status">
          No scenario path is drawn: each is paced by a liquidity growth rate, and{' '}
          {d.money_as_of ? 'the API returned none.' : 'no CBI liquidity figure is stored.'}
        </div>
      ) : d.scenarios.length < 3 || missing.length > 0 ? (
        <div className="callout callout-warn" data-testid="lq-scenario-missing" role="status">
          {drawn.length} of the three scenario paths could be drawn.
          {missing.map((s) => ` ${s.label_en}: its liquidity growth could not be measured from the stored months.`).join('')}
          {d.scenarios.length < 3 ? ` The API returned ${d.scenarios.length} scenario${d.scenarios.length === 1 ? '' : 's'}.` : ''}
        </div>
      ) : null}

      {drawn.length > 0 || d.trend ? <ScenarioTable d={d} horizon={horizon} bandKey={bandKey} unit={unit} /> : null}
    </div>
  )
}

function ScenarioTable({
  d,
  horizon,
  bandKey,
  unit
}: {
  d: LiquidityDollarResponse
  horizon: Horizon
  bandKey: string | null
  unit: DisplayUnit
}) {
  const hs = TABLE_HORIZONS.filter((h) => h <= horizon)
  const steps = new Map((d.band?.by_h ?? []).map((s) => [s.h, s]))
  const fmt = (v: number) => formatToman(v, unit, false)
  const valueAt = (path: Array<{ h: number; value: number }>, h: number) => path.find((p) => p.h === h)?.value ?? null
  // How far apart the paths end, against how wide the band is there: said
  // only from these numbers, so it changes when they do.
  const far = hs[hs.length - 1]
  const ends = d.scenarios.map((s) => valueAt(s.path, far)).filter((v): v is number => v !== null)
  const bandScn = d.scenarios.find((s) => s.key === bandKey)
  const bandEnd = bandScn ? valueAt(bandScn.path, far) : null
  const farStep = steps.get(far)
  const edges = bandEnd !== null && farStep ? bandEdges(bandEnd, farStep) : null
  return (
    <>
      <div className="table-wrap">
        <table className="table bx-table lq-table" data-testid="lq-scenario-table">
          <caption className="sr-only">Scenario path values, in {currencyLabel(unit)} per dollar</caption>
          <thead>
            <tr>
              <th scope="col">Path</th>
              <th scope="col" className="num">
                A year
              </th>
              {hs.map((h) => (
                <th key={h} scope="col" className="num">
                  {h} months
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.scenarios.map((s, i) => (
              <tr key={s.key} data-testid={`lq-scenario-${s.key}`}>
                <td className="lq-scn-name">
                  <Key color={scenarioStyle(i).color} dash /> Path {i + 1}: {s.label_en}
                  <div className="muted small">
                    <Fa>{s.label_fa}</Fa> · {s.basis}
                  </div>
                </td>
                <td className="num mono">{s.annual_growth_pct === null ? '—' : `${s.annual_growth_pct.toFixed(1)}%`}</td>
                {hs.map((h) => {
                  const v = valueAt(s.path, h)
                  const step = steps.get(h)
                  const e = s.key === bandKey && v !== null && step ? bandEdges(v, step) : null
                  return (
                    <td key={h} className="num mono">
                      {v === null ? '—' : fmt(v)}
                      {e ? (
                        <div className="muted small" title="The band at this horizon">
                          {fmt(e[0])} – {fmt(e[1])}
                        </div>
                      ) : null}
                    </td>
                  )
                })}
              </tr>
            ))}
            {d.trend ? (
              <tr data-testid="lq-scenario-trend">
                <td className="lq-scn-name">
                  <Key color="var(--muted)" dash /> The dollar’s own trend
                  <div className="muted small">mean monthly change since {d.trend.since} — a comparison line, not a liquidity path</div>
                </td>
                <td className="num mono">{`${d.trend.annual_growth_pct.toFixed(1)}%`}</td>
                {hs.map((h) => {
                  const v = valueAt(d.trend!.path, h)
                  return (
                    <td key={h} className="num mono">
                      {v === null ? '—' : fmt(v)}
                    </td>
                  )
                })}
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <p className="muted small">
        In {currencyLabel(unit)} per dollar{bandScn ? '; the figure under a value is the band at that month' : ''}.
        {ends.length > 1 && edges ? (
          <span data-testid="lq-spread">
            {' '}
            At {far} months the paths end {fmt(Math.max(...ends) - Math.min(...ends))} apart, while the band around one of
            them spans {fmt(edges[1] - edges[0])}: the uncertainty the backtest measured is{' '}
            {edges[1] - edges[0] > Math.max(...ends) - Math.min(...ends) ? 'wider than' : 'no wider than'} the difference
            between the scenarios.
          </span>
        ) : null}
      </p>
    </>
  )
}

function IndexCard({ d, calendar }: { d: LiquidityDollarResponse; calendar: CalendarMode }) {
  const rows = useMemo(() => indexRows(d.history), [d.history])
  const base = indexBasePeriod(d.history)
  const ext = indexExtremes(rows)
  const newest = rows[rows.length - 1]
  return (
    <div className="card" data-testid="lq-index">
      <div className="card-title">Dollar per unit of liquidity</div>
      {rows.length === 0 ? (
        <p className="muted small" data-testid="lq-index-empty">
          No month has both a dollar price and a liquidity figure stored, so there is no index to draw.
        </p>
      ) : (
        <>
          <p className="muted small bx-unit" data-testid="lq-index-caption">
            The dollar’s monthly average divided by liquidity, as an index
            {base ? ` equal to 100 at ${periodLabel(base)}` : ' — only its changes carry meaning'}.
            {ext
              ? ` Lowest ${(ext.min.index as number).toFixed(1)} (${periodLabel(ext.min.period)}), highest ${(
                  ext.max.index as number
                ).toFixed(1)} (${periodLabel(ext.max.period)}), newest ${(newest.index as number).toFixed(1)} (${periodLabel(
                  newest.period
                )}).`
              : ''}{' '}
            It has drifted for years instead of holding one level, so no level of it is
            treated as normal here. Before Tir 1397 liquidity is quarter-ends, and so is the index.
          </p>
          <figure className="lq-figure" aria-label="Dollar per unit of liquidity, index">
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
                <CartesianGrid stroke="var(--border)" vertical={false} />
                {monthAxis(rows)}
                <YAxis tick={{ fontSize: 11 }} width={48} domain={['auto', 'auto']} tickFormatter={(v: number) => v.toFixed(0)} />
                <Tooltip content={periodTip((v) => v.toFixed(1), calendar)} />
                {base ? <ReferenceLine y={100} stroke="var(--muted)" strokeOpacity={0.6} /> : null}
                <Line
                  type="linear"
                  dataKey="index"
                  name="Index"
                  stroke="var(--series-1)"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </figure>
        </>
      )}
    </div>
  )
}

function LiquidityDollarCard({ d, unit }: { d: LiquidityDollarResponse; unit: DisplayUnit }) {
  const ld = d.liquidity_dollar
  return (
    <div className="card" data-testid="lq-liquidity-dollar">
      <div className="card-title">
        The “liquidity dollar” <Fa>دلار نقدینگی</Fa> — and why it is a range
      </div>
      {!ld ? (
        <p className="muted small">Not available: it needs liquidity figures, and none is stored.</p>
      ) : (
        <>
          <p className="bx-verdict">
            The liquidity dollar takes the dollar in a chosen base month and scales it by how much liquidity grew since
            then. Every base month gives a different answer: at {periodLabel(ld.period)}, across{' '}
            {formatGrouped(ld.bases_considered)} base months, it runs from {formatToman(ld.by_base.min.value, unit)} to{' '}
            {formatToman(ld.by_base.max.value, unit)}. It is arithmetic about the month you pick — not a fair value, and not
            a forecast.
          </p>
          <div className="table-wrap">
            <table className="table bx-table lq-table" data-testid="lq-ld-table">
              <thead>
                <tr>
                  <th scope="col">Across base months</th>
                  <th scope="col">Base month</th>
                  <th scope="col" className="num">
                    Liquidity dollar, {periodLabel(ld.period)}
                  </th>
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    ['Lowest', ld.by_base.min],
                    ['Median', ld.by_base.median],
                    ['Highest', ld.by_base.max]
                  ] as const
                ).map(([label, v]) => (
                  <tr key={label}>
                    <td>{label}</td>
                    <td>{v.base ? periodLabel(v.base) : '—'}</td>
                    <td className="num mono">{formatToman(v.value, unit)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted small" data-testid="lq-ld-caveat">
            {ld.actual_avg !== null
              ? `The dollar’s actual average in ${periodLabel(ld.period)} was ${formatToman(ld.actual_avg, unit)}. `
              : ''}
            {ld.note}
          </p>
        </>
      )}
    </div>
  )
}

function BacktestCard({ d }: { d: LiquidityDollarResponse }) {
  const bt = d.backtest
  if (!bt) {
    return (
      <div className="card" data-testid="lq-backtest">
        <div className="card-title">How liquidity-paced paths did in a backtest</div>
        <p className="muted small" data-testid="lq-backtest-empty">
          No backtest is available: it needs the liquidity series and enough past outcomes to judge against.
        </p>
      </div>
    )
  }
  const hs = backtestHorizons(bt)
  const models = backtestModels(bt)
  const statements = backtestStatements(bt)
  return (
    <div className="card" data-testid="lq-backtest">
      <div className="card-title">How liquidity-paced paths did in a backtest</div>
      <p className="bx-verdict" data-testid="lq-verdict">
        {bt.verdict}
      </p>
      <ul className="lq-statements" data-testid="lq-statements">
        {statements.map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ul>
      <div className="table-wrap">
        <table className="table bx-table lq-table" data-testid="lq-rmse">
          <caption className="sr-only">Root mean square log error by model and horizon; lower is better</caption>
          <thead>
            <tr>
              <th scope="col">Typical miss (log error)</th>
              {hs.map((h) => (
                <th key={h} scope="col" className="num">
                  {h} months
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {models.map((m) => (
              <tr key={m} data-testid={`lq-rmse-${m}`}>
                <th scope="row">{modelLabel(m)}</th>
                {hs.map((h) => {
                  const v = bt.rmse[m]?.[h]
                  const best = bestModel(bt, h) === m
                  return (
                    <td key={h} className={`num mono ${best ? 'lq-best' : ''}`} title={best ? 'smallest at this horizon' : undefined}>
                      {v == null ? '—' : v.toFixed(3)}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">
        Target: the {bt.target}. Root mean square of log errors — 0.10 is roughly a 10% miss — lower is better; the
        smallest at each horizon is in bold. “Measurably” means a one-sided Diebold–Mariano test below p = 0.05.
        {d.band
          ? ` Forecasts were made from ${periodLabel(d.band.origins_from)} on, each using liquidity only ${d.band.lag_months} months after its month ended, as it would have been known.`
          : ''}{' '}
        The windows overlap, so the long horizons rest on few independent outcomes.
      </p>
    </div>
  )
}

// --- the notes -------------------------------------------------------------------------------------------

function NotesCard({
  agg,
  dollar,
  unit
}: {
  agg: LiquidityAggregatesResponse | null
  dollar: LiquidityDollarResponse | null
  unit: DisplayUnit
}) {
  const apiNotes = [...(agg?.notes ?? []), ...(dollar?.notes ?? [])]
  return (
    <div className="card" data-testid="lq-notes">
      <div className="card-title">Sources, units and limits</div>
      <ul className="lq-notes">
        <li>
          <strong>Sources.</strong> Liquidity (<Fa>نقدینگی</Fa>) and base money (<Fa>پایه پولی</Fa>): the Central Bank
          of Iran’s monthly Selected Economic Indicators (<Fa>گزیده آمارهای اقتصادی</Fa>) from Tir 1397, and the
          quarter-ends of CBI’s time-series database before that. The dollar:{' '}
          {dollar?.anchor ? `${dollar.anchor.source} — ${dollar.anchor.note}` : 'the stored free-market USD_IRT daily close'}.
        </li>
        <li>
          <strong>Units.</strong> CBI publishes these stocks in <Fa>هزار میلیارد ریال</Fa> (thousand billion rial)
          {agg?.display_unit_note ? `; ${agg.display_unit_note}` : ''}. With toman selected they are shown in همت, with
          rial as published — now: {moneyUnit(unit).label}. Dollar prices are in {currencyLabel(unit)}. Every money figure
          is a month-end stock, not a monthly average.
        </li>
        {(agg?.breaks ?? []).length > 0 ? (
          <li>
            <strong>Breaks.</strong> {agg!.breaks.map((b) => `${periodLabel(b.from)}: ${b.note}.`).join(' ')}
          </li>
        ) : null}
        <li>
          <strong>Publication lag.</strong> CBI states no release date. Its monthly workbooks have arrived 10 to{' '}
          {MONEY_STALE_AFTER_DAYS} days after the month they describe, often several at once; the line at the top gives
          the newest one’s.
        </li>
        <li>
          <strong>A scenario, not a forecast.</strong> Each path shows where the dollar would be if it kept pace with one
          liquidity growth rate; the backtest card says, from its own numbers, how such paths did.
        </li>
        <li>
          <strong>Correlation is not causation.</strong> Liquidity and the dollar have both risen for decades. That they
          move together over long spans does not show that one moves the other.
        </li>
        <li>
          <strong>What the paths cannot see.</strong> Sanctions, oil revenue, war and politics, expectations, Iran’s
          multiple exchange rates and the central bank’s foreign-exchange interventions. None of them is in these paths.
        </li>
        <li>
          <strong>Calendar.</strong> CBI’s periods are Jalali months and are named so whichever calendar is selected;
          dates follow the setting.
        </li>
      </ul>
      {apiNotes.length > 0 ? (
        <>
          <div className="field-label bx-subhead">From the API</div>
          <ul className="muted small lq-notes" data-testid="lq-api-notes">
            {apiNotes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  )
}

// --- the page ------------------------------------------------------------------------------------------

function Section({ title, children, testId }: { title: string; children: ReactNode; testId?: string }) {
  return (
    <section className="lq-section" aria-label={title} data-testid={testId}>
      <h3 className="section-title">{title}</h3>
      {children}
    </section>
  )
}

export default function Liquidity() {
  const { calendar, unit } = useSettings()
  const [freq, setFreq] = useState<Freq>('monthly')
  const [moneyWindow, setMoneyWindow] = useState<MoneyWindow>('10y')
  const [horizon, setHorizon] = useState<Horizon>(12)

  const agg = useApi<LiquidityAggregatesResponse>(`/liquidity/aggregates?freq=${freq}`, [freq])
  const dollar = useApi<LiquidityDollarResponse>(`/liquidity/dollar?horizon=${horizon}`, [horizon])

  // Hold the previous render while the next loads, rather than flashing a
  // spinner in place of a chart the reader was looking at.
  const lastAgg = useRef<LiquidityAggregatesResponse | null>(null)
  if (agg.data) lastAgg.current = agg.data
  const a = agg.data ?? (agg.loading ? lastAgg.current : null)
  const lastDollar = useRef<LiquidityDollarResponse | null>(null)
  if (dollar.data) lastDollar.current = dollar.data
  const d = dollar.data ?? (dollar.loading ? lastDollar.current : null)

  const newest = a ? newestOrdinal(a) : null
  const years = MONEY_WINDOWS.find((w) => w.key === moneyWindow)?.years ?? null
  const fromX = windowStart(newest, years)
  const ends = useMemo(
    () => new Map((a ? findSeries(a, CODE_M2)?.points ?? [] : []).map((p) => [p.period, p.gregorian_end])),
    [a]
  )
  const firstYear = useMemo(() => {
    const p = a ? findSeries(a, CODE_M2)?.points[0] ?? findSeries(a, CODE_BASE)?.points[0] : undefined
    return p ? p.period.slice(0, 4) : null
  }, [a])
  const ready = a !== null && hasMoneyData(a)
  const annualReady = freq === 'monthly' || (a?.annual !== undefined && !agg.loading)
  // An annual response without its annual block is a server fault: said, not spun on.
  const annualMissing = freq === 'annual' && !agg.loading && a !== null && a.annual === undefined

  return (
    <div className="page bx lq">
      <div>
        <h2 className="page-title">
          Money &amp; liquidity <span className="muted">·</span> <Fa>پایه پولی و نقدینگی</Fa>
        </h2>
        <div className="muted small">
          The Central Bank of Iran’s base money and liquidity, month by month and year by year, with the free-market dollar
          beside them and paths showing where it would stand if it kept pace with liquidity growth. Measurements and
          scenarios — not forecasts.
        </div>
      </div>

      {agg.loading && !a ? <Loading /> : null}
      {agg.error && !a ? (
        <div className="card error-box">
          <div className="card-title">The money series could not be loaded</div>
          <p className="muted small">{agg.error}</p>
        </div>
      ) : null}

      {a ? (
        <>
          <AsOf resp={a} calendar={calendar} />
          <MoneyAgeNotice resp={a} />
        </>
      ) : null}

      {a && !ready ? <NotIngested /> : null}

      {a && ready ? (
        <Section title="Base money and liquidity" testId="lq-money">
          <Headline resp={a} unit={unit} />
          <div className="row wrap bx-filter">
            <Chips<Freq>
              label="Frequency"
              options={[
                { key: 'monthly', label: 'Monthly' },
                { key: 'annual', label: 'Annual' }
              ]}
              value={freq}
              onChange={setFreq}
              testId="lq-freq"
            />
            <Chips<MoneyWindow>
              label="Years shown"
              options={MONEY_WINDOWS.map((w) => ({
                key: w.key,
                label: w.key === 'all' && firstYear ? `All (from ${firstYear})` : w.label
              }))}
              value={moneyWindow}
              onChange={setMoneyWindow}
              testId="lq-window"
            />
            <span className="muted small" data-testid="lq-unit-line">
              Values in {moneyUnit(unit).long}.
            </span>
          </div>
          {annualMissing ? (
            <div className="callout callout-warn" data-testid="lq-annual-missing" role="status">
              The API answered the annual request without its yearly figures, so there is nothing to show by year.
            </div>
          ) : !annualReady ? (
            <Loading />
          ) : (
            <div className={agg.loading ? 'bx-refetch' : undefined}>
              <LevelsCard resp={a} freq={freq} fromX={fromX} unit={unit} calendar={calendar} ends={ends} />
              <GrowthCard resp={a} freq={freq} fromX={fromX} calendar={calendar} ends={ends} />
              <MultiplierCard resp={a} freq={freq} fromX={fromX} calendar={calendar} ends={ends} />
              <div className="card" data-testid="lq-data">
                <div className="card-title">The figures</div>
                <p className="muted small bx-unit">
                  {freq === 'annual'
                    ? 'Each Jalali year: its Esfand stock and, when all twelve months are stored, the yearly average, each with its growth.'
                    : 'Each month-end stock with its growth against the month before, the same month a year before, and the previous Esfand.'}{' '}
                  In {moneyUnit(unit).label}. A dash is a growth the API did not measure; hover it for the reason. Select a
                  heading to sort.
                </p>
                {freq === 'annual' ? (
                  <AnnualTable resp={a} fromYear={fromX === undefined ? undefined : Math.floor(fromX / 12)} unit={unit} />
                ) : (
                  <MonthlyTable resp={a} fromX={fromX} unit={unit} calendar={calendar} />
                )}
              </div>
            </div>
          )}
        </Section>
      ) : null}

      <Section title="The dollar and liquidity" testId="lq-dollar-section">
        {dollar.loading && !d ? <Loading /> : null}
        {dollar.error && !d ? (
          <div className="card error-box">
            <div className="card-title">The dollar scenarios could not be loaded</div>
            <p className="muted small">{dollar.error}</p>
          </div>
        ) : null}
        {d ? (
          <>
            {d.data_age?.stale === true ? (
              <div className="callout callout-warn" data-testid="lq-dollar-stale" role="status">
                The newest dollar close stored is{' '}
                {d.anchor ? (calendar === 'jalali' ? d.anchor.jalali : d.anchor.date) : 'old'}
                {typeof d.data_age.age_days === 'number' ? `, ${formatGrouped(d.data_age.age_days)} days ago` : ''}: every
                path starts from it.{d.data_age.warning ? ` ${d.data_age.warning}` : ''}
              </div>
            ) : null}
            <DollarCard
              d={d}
              horizon={horizon}
              setHorizon={setHorizon}
              loading={dollar.loading}
              unit={unit}
              calendar={calendar}
            />
            <IndexCard d={d} calendar={calendar} />
            <LiquidityDollarCard d={d} unit={unit} />
            <BacktestCard d={d} />
          </>
        ) : null}
      </Section>

      <NotesCard agg={a} dollar={d} unit={unit} />
    </div>
  )
}
