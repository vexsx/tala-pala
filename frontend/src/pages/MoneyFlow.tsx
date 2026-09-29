import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useApi } from '../hooks/useApi'
import { useSettings } from '../lib/settings'
import Loading from '../components/Loading'
import { formatDate, formatGrouped, formatPct, pctClass, shortDate } from '../lib/format'
import { formatTomanScaled } from '../lib/bourse'
import { BourseAgeNotice } from './Bourse'
import type {
  SectorFlowBlock,
  SectorFlowChecks,
  SectorFlowCompany,
  SectorFlowGroupCoverage,
  SectorFlowSector,
  SectorFlowShare,
  SectorFlowSharesResponse,
  SectorFlowsResponse,
  SectorFlowWindowCoverage,
  TieredFlowSummary
} from '../api/types'

/**
 * Money flow across the whole Tehran market: where individuals (حقیقی) and
 * institutions (حقوقی) traded against each other, sector by sector and share
 * by share, over the newest 1, 5, 20 and 60 market sessions.
 *
 * WHAT THIS PAGE WILL NOT LET A READER MISTAKE
 *
 *  1. A TRANSFER FOR NEW MONEY. Every trade has a buyer and a seller, so
 *     individuals' net buying is exactly institutions' net selling. The page
 *     states the institutional side beside every market figure and says in
 *     words that nothing here is money arriving from outside the market. The
 *     rotation between sectors is their SHARE of traded value and its change.
 *  2. A SAMPLE FOR THE MARKET. A session counts only when flow is stored for
 *     at least 90% of the shares TSETMC's day file shows trading, by count
 *     and by value; until then the page says so and sums nothing (the roster
 *     table lives on the Tehran market page, labelled as the roster), and
 *     once it does, every window states the part of the market it rests on.
 *     A traded share with no flow stored is "flow not stored", never "did
 *     not trade".
 *  3. AN UNCHECKED FIGURE FOR A CHECKED ONE. Every figure rests on share-
 *     sessions in four tiers (checked against a session value, identities
 *     only, excluded, no trade) and the counts are on screen wherever a sum is.
 *  4. A LINK THAT GOES NOWHERE. Only roster shares have a share page; every
 *     other symbol is plain text.
 *
 * Nothing is computed here beyond formatting and sorting: every sum, share,
 * change and check arrives from the API, measured once in Go.
 */

const WINDOWS = ['1', '5', '20', '60'] as const
type WindowKey = (typeof WINDOWS)[number]
const DEFAULT_WINDOW: WindowKey = '5'

function Fa({ children }: { children: string }) {
  return (
    <span className="bidi-fa" lang="fa" dir="rtl">
      {children}
    </span>
  )
}

function Pct({ value, digits = 1, title }: { value: number | null | undefined; digits?: number; title?: string }) {
  return (
    <span className={`mono ${pctClass(value)}`} title={title}>
      {formatPct(value, { digits })}
    </span>
  )
}

// --- formatting ---------------------------------------------------------------------

/** A signed toman amount at the market's scale: "+1.2T toman". */
export function signedToman(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value > 0 ? '+' : ''}${formatTomanScaled(value)}`
}

/** A change in percentage points, always signed: "+0.42 pp". */
export function signedPp(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)} pp`
}

export function sectorLabel(s: { sector_code: string; name_en: string }): string {
  if (s.name_en) return s.name_en
  return s.sector_code ? `Sector ${s.sector_code}` : 'Unclassified'
}

function windowLabel(w: string): string {
  return w === '1' ? '1 session' : `${w} sessions`
}

/** "1 share", "24 shares". */
export function countOf(n: number, noun: string): string {
  return `${formatGrouped(n)} ${noun}${n === 1 ? '' : 's'}`
}

/** A percentage the API measured, or a dash. */
function pctText(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? '—' : `${v.toFixed(digits)}%`
}

/**
 * What part of the market a window's figures rest on, against TSETMC's own day
 * files: "flow for 96.0% of the traded share-sessions (98.1% of their value)".
 */
export function coverageLine(c: SectorFlowWindowCoverage | undefined): string | null {
  if (!c) return null
  return (
    `Flow is stored for ${pctText(c.share_pct)} of the ${formatGrouped(c.day_file_share_sessions)} ` +
    `share-sessions TSETMC's day files show trading (${pctText(c.value_pct)} of their traded value)`
  )
}

/**
 * A share's, company's or sector's OWN coverage, when the day files list
 * anything its flow is missing: "flow for 25 of 30 traded share-sessions
 * (40.0% of their value)". Null when nothing is missing. `short` is below the
 * 90% a share of traded value needs, where the API withholds that share.
 */
export function groupCoverage(
  c: SectorFlowGroupCoverage | null | undefined
): { text: string; short: boolean } | null {
  if (!c || c.day_file_share_sessions === 0) return null
  const missing = c.with_flow < c.day_file_share_sessions || (c.value_pct !== null && c.value_pct < 100)
  if (!missing) return null
  const short = (c.share_pct ?? 100) < 90 || (c.value_pct ?? 100) < 90
  return {
    text:
      `flow for ${formatGrouped(c.with_flow)} of ${countOf(c.day_file_share_sessions, 'traded share-session')} ` +
      `(${pctText(c.value_pct)} of their value)`,
    short
  }
}

function CoverageMark({ c, reason, testId }: { c: SectorFlowGroupCoverage | null | undefined; reason?: string; testId?: string }) {
  const cov = groupCoverage(c)
  if (!cov) return null
  return cov.short ? (
    <div>
      <span className="badge badge-warn bx-badge" data-testid={testId} title={reason ?? cov.text}>
        {cov.text}
      </span>
    </div>
  ) : (
    <div className="muted small" data-testid={testId}>
      {cov.text}
    </div>
  )
}

/** A value share, or a dash that says why there is none. */
function ShareCell({ value, reason }: { value: number | null | undefined; reason?: string }) {
  return (
    <td className="num mono" title={value == null ? reason : undefined}>
      {value == null ? '—' : `${value.toFixed(1)}%`}
    </td>
  )
}

// --- the heatmap's diverging scale ----------------------------------------------------------

/**
 * Seven classes of net individual flow as a share of traded value: three steps
 * of `--flow-in` for individuals buying more, three of `--flow-out` for selling
 * more, and a neutral gray between ±1% — "nothing to speak of", never a hue.
 * Fixed thresholds rather than the data's own range, so the same colour means
 * the same thing in every window and on every visit.
 */
export const HEAT_CLASSES: Array<{ cls: string; label: string }> = [
  { cls: 'mf-h-n3', label: '≤ −6%' },
  { cls: 'mf-h-n2', label: '−6 to −3%' },
  { cls: 'mf-h-n1', label: '−3 to −1%' },
  { cls: 'mf-h-0', label: 'within ±1%' },
  { cls: 'mf-h-p1', label: '+1 to +3%' },
  { cls: 'mf-h-p2', label: '+3 to +6%' },
  { cls: 'mf-h-p3', label: '≥ +6%' }
]

export function heatClass(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return 'mf-h-na'
  const a = Math.abs(pct)
  if (a < 1) return 'mf-h-0'
  const step = a < 3 ? 1 : a < 6 ? 2 : 3
  return `mf-h-${pct > 0 ? 'p' : 'n'}${step}`
}

// --- sorting -------------------------------------------------------------------------------

export type SectorSortKey = 'name' | 'value' | 'share' | 'share_change' | 'net' | 'net_pct' | 'buyer_power' | 'index'

export function sectorSortValue(s: SectorFlowSector, w: string, key: SectorSortKey): number | string | null {
  const win = s.windows[w]
  if (key === 'name') return sectorLabel(s)
  if (!win) return null
  switch (key) {
    case 'value':
      return win.total_value_toman
    case 'share':
      return win.value_share_pct
    case 'share_change':
      return win.value_share_change_pp
    case 'net':
      return win.net_individual_toman
    case 'net_pct':
      return win.net_individual_pct_of_value
    case 'buyer_power':
      return win.buyer_power
    case 'index':
      return win.index_return_pct
  }
}

/** Sort without letting a missing figure pose as the smallest or the largest. */
export function sortSectors(
  sectors: SectorFlowSector[],
  w: string,
  key: SectorSortKey,
  desc: boolean
): SectorFlowSector[] {
  return [...sectors].sort((a, b) => {
    const va = sectorSortValue(a, w, key)
    const vb = sectorSortValue(b, w, key)
    if (va === null && vb === null) return a.sector_code.localeCompare(b.sector_code)
    if (va === null) return 1
    if (vb === null) return -1
    if (typeof va === 'string' || typeof vb === 'string') {
      const c = String(va).localeCompare(String(vb))
      return desc ? -c : c
    }
    return desc ? vb - va : va - vb
  })
}

// --- small marks ---------------------------------------------------------------------------

/**
 * Net individual flow as a diverging bar about a 2px zero gap: blue to the
 * right when individuals bought more, red to the left when they sold more,
 * length against the largest sector in the same window.
 */
function DivergingBar({ value, max }: { value: number | null; max: number }) {
  const pct = value === null || max <= 0 ? 0 : Math.min(100, (Math.abs(value) / max) * 100)
  const label =
    value === null
      ? 'no checked row'
      : value === 0
        ? 'no net individual flow'
        : `${value > 0 ? 'individuals bought more' : 'individuals sold more'}: ${signedToman(value)}`
  return (
    <span className="mf-dbar" role="img" aria-label={label} title={label}>
      <span className="mf-dbar-half mf-dbar-neg">
        {value !== null && value < 0 ? <span style={{ width: `${pct}%` }} /> : null}
      </span>
      <span className="mf-dbar-half mf-dbar-pos">
        {value !== null && value > 0 ? <span style={{ width: `${pct}%` }} /> : null}
      </span>
    </span>
  )
}

/**
 * A sector's share of the market's traded value over the twelve five-session
 * blocks, one hue, each row on its own scale — the shape of its rotation, not
 * a comparison between rows (the column beside it carries the level).
 */
function ShareSpark({ blocks, calendar }: { blocks: SectorFlowBlock[]; calendar: 'jalali' | 'gregorian' }) {
  const pts = blocks.map((b) => b.value_share_pct)
  const vals = pts.filter((v): v is number => v !== null)
  if (vals.length < 2) return <span className="muted small">—</span>
  const w = 84
  const h = 22
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const span = max - min || 1
  const xy = pts
    .map((v, i) => (v === null ? null : [2 + (i / (pts.length - 1)) * (w - 4), h - 3 - ((v - min) / span) * (h - 6)]))
    .filter((p): p is number[] => p !== null)
  const first = blocks.find((b) => b.value_share_pct !== null)
  const last = [...blocks].reverse().find((b) => b.value_share_pct !== null)
  const label =
    first && last
      ? `Share of traded value by five-session block: ${first.value_share_pct?.toFixed(1)}% (${formatDate(first.to, calendar)}) → ${last.value_share_pct?.toFixed(1)}% (${formatDate(last.to, calendar)})`
      : 'Share of traded value by five-session block'
  const [lx, ly] = xy[xy.length - 1]
  return (
    <svg className="mf-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label}>
      <title>{label}</title>
      <polyline
        points={xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ')}
        fill="none"
        stroke="var(--series-1)"
        strokeWidth={1.5}
        strokeLinejoin="round"
      />
      <circle cx={lx} cy={ly} r={2} fill="var(--series-1)" />
    </svg>
  )
}

/** The four tiers of the rows behind a figure, compactly. */
function Tiers({ s }: { s: TieredFlowSummary }) {
  const title =
    `${formatGrouped(s.bar_checked)} share-session(s) checked against a session value, ` +
    `${formatGrouped(s.identity_only)} on the identities only, ${formatGrouped(s.excluded)} excluded ` +
    `for failing a check, ${formatGrouped(s.no_trade)} with no trade`
  return (
    <span className="mf-tiers mono small" title={title}>
      {formatGrouped(s.bar_checked)} · {formatGrouped(s.identity_only)} · {formatGrouped(s.excluded)}
    </span>
  )
}

function ShareSymbol({
  symbol,
  nameFa,
  inRoster,
  rosterSymbol
}: {
  symbol: string
  nameFa: string
  inRoster: boolean
  rosterSymbol?: string
}) {
  return (
    <>
      {inRoster && rosterSymbol ? (
        <Link className="stk-symbol-link" to={`/stocks/${encodeURIComponent(rosterSymbol)}`}>
          <Fa>{symbol}</Fa>
        </Link>
      ) : (
        <span className="mf-plain-symbol" title="Not on this deployment's roster, so there is no share page">
          <Fa>{symbol}</Fa>
        </span>
      )}
      {nameFa ? (
        <div className="muted small">
          <Fa>{nameFa}</Fa>
        </div>
      ) : null}
    </>
  )
}

/**
 * A share listed inside the window: its listing session was an offering, whose
 * individual buying is an allocation, and its price change is measured from
 * its first close. Said beside the name so an inflow list is not read as
 * individuals piling into an existing share.
 */
function ListingBadge({ date }: { date?: string }) {
  const { calendar } = useSettings()
  if (!date) return null
  return (
    <span
      className="badge badge-warn bx-badge"
      data-testid="mf-listing"
      title="Listed inside this window: its listing session was an offering, whose individual buying is an allocation. Its price change is measured from its first close."
    >
      listed {formatDate(date, calendar)}
    </span>
  )
}

function Tile({ label, value, hint, testId }: { label: string; value: string; hint?: ReactNode; testId?: string }) {
  return (
    <div className="card bx-tile" data-testid={testId}>
      <div className="field-label">{label}</div>
      <div className="bx-tile-value">{value}</div>
      {hint ? <div className="muted small">{hint}</div> : null}
    </div>
  )
}

// --- the market ------------------------------------------------------------------------------

function MarketTiles({
  m,
  window: w,
  coverage
}: {
  m: TieredFlowSummary
  window: string
  coverage?: SectorFlowWindowCoverage
}) {
  const cov = coverageLine(coverage)
  return (
    <div className="bx-tiles" data-testid="mf-tiles">
      <Tile
        testId="mf-tile-value"
        label="Traded value"
        value={formatTomanScaled(m.total_value_toman)}
        hint={`${countOf(m.instruments_traded, 'share')} with stored flow traded over ${windowLabel(
          w
        )}; every board counted.${cov ? ` ${cov}.` : ''}`}
      />
      <Tile
        testId="mf-tile-net"
        label="Net individual flow"
        value={signedToman(m.net_individual_toman)}
        hint={
          <>
            {formatPct(m.net_individual_pct_of_value, { digits: 1 })} of traded value. Institutions:{' '}
            <span className="mono">{signedToman(m.net_institutional_toman)}</span> — the same trades, the other side.
          </>
        }
      />
      <Tile
        testId="mf-tile-breadth"
        label="Shares with inflow / outflow"
        value={`${formatGrouped(m.inflow_instruments)} / ${formatGrouped(m.outflow_instruments)}`}
        hint="Shares whose individuals bought more / sold more over the window."
      />
      <Tile
        testId="mf-tile-power"
        label="Individual buyer power"
        value={m.buyer_power === null ? '—' : m.buyer_power.toFixed(2)}
        hint={
          `Average individual buy ticket ÷ average sell ticket, per trader-session. Ticket sizes, not a forecast.` +
          (m.listing_sessions > 0
            ? ` Leaves out ${countOf(m.listing_sessions, 'listing session')}: an offering's allocation — over a million buy tickets against almost none sold — would swamp every other session's tickets.`
            : '')
        }
      />
      <Tile
        testId="mf-tile-tiers"
        label="Rows counted"
        value={`${formatGrouped(m.consistent)} of ${formatGrouped(m.rows)}`}
        hint={`${formatGrouped(m.bar_checked)} checked against a session value · ${formatGrouped(
          m.identity_only
        )} identities only · ${formatGrouped(m.excluded)} excluded · ${formatGrouped(m.no_trade)} no trade`}
      />
    </div>
  )
}

// --- the sector table --------------------------------------------------------------------------

const SECTOR_COLUMNS: Array<{ key: SectorSortKey; label: string; title?: string }> = [
  { key: 'value', label: 'Traded value' },
  { key: 'share', label: 'Share of market', title: "The sector's part of the market's traded value" },
  { key: 'share_change', label: 'Δ vs previous', title: 'Change in share of traded value against the window of equal length before, in percentage points' },
  { key: 'net', label: 'Net individual', title: 'Individuals’ buying minus their selling; institutions’ net is exactly the negative' },
  { key: 'net_pct', label: '% of value' },
  { key: 'buyer_power', label: 'Buyer power' },
  { key: 'index', label: 'Sector index', title: "The sector's bourse index over the same sessions, where one exists" }
]

function SectorTable({
  data,
  window: w,
  calendar
}: {
  data: SectorFlowsResponse
  window: WindowKey
  calendar: 'jalali' | 'gregorian'
}) {
  const [sort, setSort] = useState<{ key: SectorSortKey; desc: boolean }>({ key: 'value', desc: true })
  const [open, setOpen] = useState<string | null>(null)
  const rows = useMemo(() => sortSectors(data.sectors, w, sort.key, sort.desc), [data.sectors, w, sort])
  const maxNet = useMemo(
    () => Math.max(0, ...data.sectors.map((s) => Math.abs(s.windows[w]?.net_individual_toman ?? 0))),
    [data.sectors, w]
  )
  // A button inside each header, as on the Stocks screen, so the sort is
  // reachable from the keyboard and announced as a control.
  const header = (key: SectorSortKey, label: string, title?: string, cls = 'num') => (
    <th
      key={key}
      className={cls || undefined}
      scope="col"
      aria-sort={sort.key === key ? (sort.desc ? 'descending' : 'ascending') : 'none'}
    >
      <button
        type="button"
        className="th-sort mf-sort"
        title={title}
        onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : key !== 'name' }))}
      >
        {label}
        {sort.key === key ? (
          <span className="th-sort-mark" aria-hidden="true">
            {sort.desc ? '↓' : '↑'}
          </span>
        ) : null}
      </button>
    </th>
  )
  const columns = SECTOR_COLUMNS.length + 4
  return (
    <div className="card" data-testid="mf-sectors">
      <div className="card-title">Sectors — where individuals bought and sold</div>
      <p className="muted small bx-unit">
        Every TSETMC sector with stored flow, over {windowLabel(w)}. The bar is net individual flow against the largest
        sector's;{' '}
        {data.blocks.length > 0
          ? `the line is the sector's share of traded value over the last ${countOf(
              data.blocks.length,
              'five-session block'
            )}, each on its own scale.`
          : 'the trend line needs five market sessions, and fewer are stored.'}{' '}
        <em>Checks</em> counts the share-sessions behind each row: checked against a session value · identities only ·
        excluded. A sector with nothing summed shows no share of the market (—), not 0%; so does one whose flow covers
        less than 90% of what TSETMC's day files show it trading (its coverage is marked, and the dash says why).
        Select a sector for its shares.
      </p>
      <div className="bx-legend" aria-hidden="true">
        <span className="bx-legend-item">
          <span className="mf-key" style={{ background: 'var(--flow-in)' }} /> individuals bought more
        </span>
        <span className="bx-legend-item">
          <span className="mf-key" style={{ background: 'var(--flow-out)' }} /> individuals sold more
        </span>
        <span className="bx-legend-item">
          <span className="bx-key" style={{ borderTopColor: 'var(--series-1)' }} /> share of traded value, 12 blocks
        </span>
      </div>
      <div className="table-wrap">
        <table className="table bx-table mf-table" data-testid="mf-sector-table">
          <thead>
            <tr>
              {header('name', 'Sector', undefined, '')}
              {SECTOR_COLUMNS.slice(0, 2).map((c) => header(c.key, c.label, c.title))}
              <th>Trend</th>
              {SECTOR_COLUMNS.slice(2).map((c) => header(c.key, c.label, c.title))}
              <th className="num" title="Shares whose individuals bought more / sold more">
                Shares in / out
              </th>
              <th className="num" title="Share-sessions: checked against a session value · identities only · excluded">
                Checks
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => {
              const win = s.windows[w]
              const expanded = open === s.sector_code
              const expandable = s.sector_code !== ''
              return (
                <Fragment key={s.sector_code}>
                  <tr data-testid={`mf-sector-${s.sector_code}`} className={expanded ? 'mf-open' : undefined}>
                    <td className="bx-name">
                      {expandable ? (
                        <button
                          className="link-btn"
                          aria-expanded={expanded}
                          onClick={() => setOpen(expanded ? null : s.sector_code)}
                          title="Show this sector's shares"
                        >
                          {sectorLabel(s)}
                        </button>
                      ) : (
                        sectorLabel(s)
                      )}
                      <div className="muted small">
                        <span className="mono">{s.sector_code}</span> ·{' '}
                        <span title="Shares with stored flow in the loaded sessions, of the shares in the sector's list">
                          {formatGrouped(s.instruments)} of {countOf(s.shares, 'share')} with flow
                        </span>
                        {s.name_fa ? (
                          <>
                            {' '}
                            · <Fa>{s.name_fa}</Fa>
                          </>
                        ) : null}
                      </div>
                      <CoverageMark c={win?.coverage} reason={win?.value_share_reason} testId={`mf-sector-coverage-${s.sector_code}`} />
                    </td>
                    <td className="num mono">{formatTomanScaled(win?.total_value_toman)}</td>
                    <ShareCell value={win?.value_share_pct} reason={win?.value_share_reason} />
                    <td>
                      <ShareSpark blocks={s.blocks} calendar={calendar} />
                    </td>
                    <td
                      className="num mono"
                      title={
                        win?.previous_value_share_pct != null
                          ? `previous window ${win.previous_value_share_pct.toFixed(1)}%`
                          : win?.value_share_reason
                      }
                    >
                      {signedPp(win?.value_share_change_pp)}
                    </td>
                    <td className="num mf-net-cell">
                      <DivergingBar value={win?.net_individual_toman ?? null} max={maxNet} />
                      <span className="mono">{signedToman(win?.net_individual_toman)}</span>
                    </td>
                    <td className="num mono">{formatPct(win?.net_individual_pct_of_value, { digits: 1 })}</td>
                    <td className="num mono">{win?.buyer_power == null ? '—' : win.buyer_power.toFixed(2)}</td>
                    <td className="num">
                      <Pct value={win?.index_return_pct} title={win?.index_return_reason} />
                    </td>
                    <td className="num mono">
                      {win ? `${formatGrouped(win.inflow_instruments)} / ${formatGrouped(win.outflow_instruments)}` : '—'}
                    </td>
                    <td className="num">{win ? <Tiers s={win} /> : '—'}</td>
                  </tr>
                  {expanded ? (
                    <tr className="mf-expand">
                      <td colSpan={columns}>
                        <SectorShares code={s.sector_code} window={w} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SectorShares({ code, window: w }: { code: string; window: WindowKey }) {
  const path = `/bourse/sector-flows/${code}?window=${w}`
  const res = useApi<SectorFlowSharesResponse>(path, [path])
  if (res.error) return <p className="muted small">{res.error}</p>
  if (!res.data) return <Loading />
  const d = res.data
  return (
    <div className="mf-shares" data-testid={`mf-shares-${code}`}>
      <div className="field-label">
        {sectorLabel(d)} — {countOf(d.count, 'share')} over {windowLabel(d.window)}
      </div>
      <p className="muted small bx-unit">
        Each board is its own row. <em>Share of sector</em> is the share's part of the sector's traded value. A share
        TSETMC's day files show trading with no flow stored for those sessions says <em>flow not stored</em> — for how
        many of its sessions, when it is some of them; a listed share that did not trade in the window is listed with
        empty figures. Price change is chained through each
        session's reference price, so a capital increase is not read as a fall; it is measured from the first close of
        a share listed inside the window, and is empty when a traded session has no stored close.
      </p>
      <div className="table-wrap">
        <table className="table bx-table mf-table">
          <thead>
            <tr>
              <th>Share</th>
              <th>Board</th>
              <th className="num">Traded value</th>
              <th className="num" title="The share's part of the sector's traded value">
                Share of sector
              </th>
              <th className="num">Net individual</th>
              <th className="num">% of value</th>
              <th className="num">Buyer power</th>
              <th className="num" title="Chained from the official close against each session's reference price">
                Price change
              </th>
              <th className="num" title="Share-sessions: checked against a session value · identities only · excluded">
                Checks
              </th>
            </tr>
          </thead>
          <tbody>
            {d.items.map((it: SectorFlowShare) => (
              <tr key={it.ins_code} data-testid={`mf-share-${it.ins_code}`}>
                <td className="bx-name">
                  <ShareSymbol symbol={it.symbol} nameFa={it.name_fa} inRoster={it.in_roster} rosterSymbol={it.roster_symbol} />
                </td>
                <td className="small">
                  {it.board}
                  {it.market !== 'bourse' ? ` · ${it.market}` : ''}
                  {!it.listed ? ' · delisted' : ''}
                  <ListingBadge date={it.listing_session} />
                </td>
                <td className="num mono">
                  {it.summary.rows === 0 && it.flow_not_stored_sessions > 0 ? (
                    <span
                      className="muted small"
                      data-testid={`mf-flow-not-stored-${it.ins_code}`}
                      title={`TSETMC's day files show it trading on ${countOf(
                        it.traded_sessions,
                        'session'
                      )} of this window; no flow is stored for them`}
                    >
                      flow not stored
                    </span>
                  ) : (
                    <>
                      {formatTomanScaled(it.summary.total_value_toman)}
                      {/* Partly stored: the value above is summed over the
                          sessions whose flow is stored, and says so. */}
                      {it.flow_not_stored_sessions > 0 ? (
                        <div
                          className="muted small"
                          data-testid={`mf-flow-partial-${it.ins_code}`}
                          title={`TSETMC's day files show it trading on ${countOf(
                            it.traded_sessions,
                            'session'
                          )} of this window; no flow is stored for ${it.flow_not_stored_sessions} of them, and the figures leave them out`}
                        >
                          flow not stored for {formatGrouped(it.flow_not_stored_sessions)} of{' '}
                          {countOf(it.traded_sessions, 'session')}
                        </div>
                      ) : null}
                    </>
                  )}
                </td>
                <ShareCell value={it.summary.value_share_pct} reason={it.summary.value_share_reason} />
                <td className="num mono">{signedToman(it.summary.net_individual_toman)}</td>
                <td className="num mono">{formatPct(it.summary.net_individual_pct_of_value, { digits: 1 })}</td>
                <td className="num mono">{it.summary.buyer_power == null ? '—' : it.summary.buyer_power.toFixed(2)}</td>
                <td className="num">
                  <Pct value={it.price_change_pct} title={it.price_change_reason ?? it.price_change_note} />
                </td>
                <td className="num">
                  <Tiers s={it.summary} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// --- the heatmap ---------------------------------------------------------------------------------

function HeatCell({
  b,
  who,
  calendar,
  market = false
}: {
  b: SectorFlowBlock | undefined
  who: string
  calendar: 'jalali' | 'gregorian'
  market?: boolean
}) {
  if (!b) return <td className="mf-heat-cell mf-h-na">—</td>
  const pct = b.net_individual_pct_of_value
  const share = market || b.value_share_pct == null ? '' : `; ${b.value_share_pct.toFixed(1)}% of the market's traded value`
  const title =
    `${who}, ${formatDate(b.from, calendar)} → ${formatDate(b.to, calendar)}: ` +
    (pct === null
      ? 'no checked row'
      : `net individual ${signedToman(b.net_individual_toman)}, ${formatPct(pct, { digits: 1 })} of ` +
        `${formatTomanScaled(b.total_value_toman)} traded${share}` +
        (b.excluded ? `; ${b.excluded} row(s) excluded` : ''))
  return (
    <td className={`mf-heat-cell ${heatClass(pct)}`} title={title}>
      {pct === null ? '—' : `${pct > 0 ? '+' : ''}${pct.toFixed(1)}`}
    </td>
  )
}

function Heatmap({ data, calendar }: { data: SectorFlowsResponse; calendar: 'jalali' | 'gregorian' }) {
  // Largest sectors first, by their share of the longest window stored (sixty
  // sessions once there are that many), so the order does not move when the
  // window chips do. Before sixty sessions exist the sixty-session figure is
  // empty for every sector, and ordering by it would be ordering by code.
  const rows = useMemo(() => {
    const longest = [...WINDOWS].reverse().find((k) => data.sessions[k]?.available) ?? '60'
    return sortSectors(data.sectors, longest, 'share', true)
  }, [data.sectors, data.sessions])
  if (data.blocks.length === 0) {
    // Said, not silently skipped: the table above promises a trend line too.
    return (
      <div className="card" data-testid="mf-heatmap-empty">
        <div className="card-title">Net individual flow, five sessions at a time</div>
        <p className="muted small">
          The heatmap needs five market sessions; {formatGrouped(data.coverage.sessions_available)}{' '}
          {data.coverage.sessions_available === 1 ? 'is' : 'are'} stored.
        </p>
      </div>
    )
  }
  return (
    <div className="card" data-testid="mf-heatmap">
      <div className="card-title">Net individual flow, five sessions at a time</div>
      <p className="muted small bx-unit">
        The newest {formatGrouped(data.blocks.length * 5)} market sessions in {formatGrouped(data.blocks.length)} blocks
        of five, oldest on the left. Each cell is net individual flow as a percentage of the block's traded value; hover a
        cell for the amounts.
      </p>
      <div className="mf-heat-legend" data-testid="mf-heat-legend">
        <span className="small muted">individuals sold more</span>
        {HEAT_CLASSES.map((c) => (
          <span key={c.cls} className="mf-heat-key">
            <span className={`mf-heat-swatch ${c.cls}`} aria-hidden="true" />
            <span className="small">{c.label}</span>
          </span>
        ))}
        <span className="small muted">individuals bought more</span>
      </div>
      <div className="table-wrap">
        <table className="table bx-table mf-heat" aria-label="Net individual flow as % of traded value, by sector and five-session block">
          <thead>
            <tr>
              <th scope="col">Sector</th>
              {data.blocks.map((b) => (
                <th key={b.to} className="num mf-heat-head" title={`${formatDate(b.from, calendar)} → ${formatDate(b.to, calendar)}`}>
                  {shortDate(b.to, calendar)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="mf-heat-market">
              <th scope="row">
                <strong>Whole market</strong>
              </th>
              {data.market_blocks.map((b) => (
                <HeatCell key={b.to} b={b} who="Whole market" calendar={calendar} market />
              ))}
            </tr>
            {rows.map((s) => (
              <tr key={s.sector_code}>
                {/* A row header, so a screen reader names the sector with each cell. */}
                <th scope="row" className="mf-heat-name">
                  {sectorLabel(s)}
                  {s.name_fa ? (
                    <span className="muted small">
                      {' '}
                      <Fa>{s.name_fa}</Fa>
                    </span>
                  ) : null}
                </th>
                {data.blocks.map((blk, i) => (
                  <HeatCell key={blk.to} b={s.blocks[i]} who={sectorLabel(s)} calendar={calendar} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// --- the top lists ---------------------------------------------------------------------------------

function TopList({
  title,
  items,
  testId,
  empty
}: {
  title: string
  items: SectorFlowCompany[]
  testId: string
  empty: string
}) {
  return (
    <div className="card" data-testid={testId}>
      <div className="card-title">{title}</div>
      {items.length === 0 ? (
        <p className="muted small">{empty}</p>
      ) : (
        <div className="table-wrap">
          <table className="table bx-table mf-table">
            <thead>
              <tr>
                <th className="num">#</th>
                <th>Company</th>
                <th>Sector</th>
                <th className="num">Net individual</th>
                <th className="num">% of value</th>
                <th className="num">Traded value</th>
                <th className="num">Buyer power</th>
                <th className="num" title="On the board shown, chained through each session's reference price">
                  Price change
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((c, i) => (
                <tr key={c.company_code || c.ins_code} data-testid={`mf-company-${c.ins_code}`}>
                  <td className="num mono muted">{i + 1}</td>
                  <td className="bx-name">
                    <ShareSymbol symbol={c.symbol} nameFa={c.name_fa} inRoster={c.in_roster} rosterSymbol={c.roster_symbol} />
                    {c.boards.length > 1 ? (
                      <span className="badge badge-info bx-badge" title="Every board of the company is summed">
                        {c.boards.join(' + ')}
                      </span>
                    ) : null}
                    <ListingBadge date={c.listing_session} />
                    <CoverageMark c={c.summary.coverage} reason={c.summary.value_share_reason} testId={`mf-company-coverage-${c.ins_code}`} />
                  </td>
                  <td className="small">{sectorLabel({ sector_code: c.sector_code, name_en: c.sector_name_en })}</td>
                  <td className="num mono">{signedToman(c.summary.net_individual_toman)}</td>
                  <td className="num mono">{formatPct(c.summary.net_individual_pct_of_value, { digits: 1 })}</td>
                  <td className="num mono">{formatTomanScaled(c.summary.total_value_toman)}</td>
                  <td className="num mono">{c.summary.buyer_power == null ? '—' : c.summary.buyer_power.toFixed(2)}</td>
                  <td className="num">
                    <Pct value={c.price_change_pct} title={c.price_change_reason ?? c.price_change_note} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// --- what it is and is not ---------------------------------------------------------------------------

/**
 * What the page's figures are, in plain words, and then the API's own notes.
 * The notes already say that the flow nets to zero, what each check is, and
 * that nothing here is a forecast — this card used to say each of those again
 * in its own words before listing them — so the prose here is only what the
 * notes do not: what a positive number means, and where rotation lives.
 */
function Explainer({ checks, notes }: { checks: SectorFlowChecks; notes: string[] }) {
  return (
    <div className="card mf-explainer" data-testid="mf-explainer">
      <div className="card-title">What net individual flow is — and what it is not</div>
      <p className="bx-verdict">
        TSETMC reports, for every share and every session, how much individuals (<Fa>حقیقی</Fa>) bought and sold and how
        much institutions (<Fa>حقوقی</Fa>) did. <strong>Net individual flow</strong> is individuals' buying minus their
        selling. Positive means individuals bought more than they sold — so institutions sold them exactly that much.{' '}
        <strong>It is not new money entering the market, and it is not a forecast.</strong>
      </p>
      <p className="bx-verdict">
        What <em>does</em> move between sectors is attention: a sector's <strong>share of the market's traded value</strong>.
        Its change against the window of equal length before (<em>Δ vs previous</em>) is the rotation. Every share-session
        is checked first (identities to {checks.identity_tolerance_pct}%, session values to{' '}
        {checks.session_value_tolerance_pct}%); the notes below say how.
      </p>
      <ul className="muted small mf-notes">
        {notes.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
    </div>
  )
}

function NotIngested({ data, calendar }: { data: SectorFlowsResponse; calendar: 'jalali' | 'gregorian' }) {
  const c = data.coverage
  const stored = c.newest_stored
  const dayFile = c.newest_day_file
  return (
    <div className="card callout callout-warn" data-testid="mf-not-ingested" role="status">
      <div className="card-title">Market-wide flows are not stored for any session yet</div>
      <p className="bx-verdict">
        A date counts as a market session only when TSETMC's day file for it shows at least{' '}
        {formatGrouped(c.min_traded_shares)} shares trading and flow is stored for at least {c.min_coverage_pct}% of them,
        by count and by traded value.{' '}
        {stored?.date ? (
          <>
            The newest stored date, {formatDate(stored.date, calendar)}, is not one: {stored.reason}. A sample of the market —
            the roster alone, a part of the shares, or an ingest still under way — is not the market, so nothing is summed
            here.
          </>
        ) : (
          <>No money-flow row and no day file is stored at all, so nothing is summed here.</>
        )}
      </p>
      {/* The newest stored date can be a roster-only flow ingest with no day
          file; the day files that ARE stored, and how far short their flow
          falls, are the part an operator can act on. */}
      {stored?.date && dayFile?.date && dayFile.date !== stored.date ? (
        <p className="bx-verdict" data-testid="mf-newest-day-file">
          The newest date with a whole-market day file, {formatDate(dayFile.date, calendar)}, is not one either:{' '}
          {dayFile.reason}. {countOf(c.day_files_stored, 'stored date')} {c.day_files_stored === 1 ? 'has' : 'have'} a day
          file.
        </p>
      ) : stored?.date && !dayFile ? (
        <p className="bx-verdict" data-testid="mf-newest-day-file">
          No whole-market day file is stored at all.
        </p>
      ) : null}
      <p className="muted small">
        The roster's own table — {countOf(c.roster_instruments, 'share')}, labelled as the roster — is on the{' '}
        <Link to="/bourse">Tehran market</Link> page. Market-wide flows arrive with the off-server fetch (
        <code>{data.data_age.refresh_command}</code>).
      </p>
    </div>
  )
}

// --- the page ---------------------------------------------------------------------------------------

export default function MoneyFlow() {
  const { calendar } = useSettings()
  const res = useApi<SectorFlowsResponse>('/bourse/sector-flows')
  const [chosen, setChosen] = useState<WindowKey>(DEFAULT_WINDOW)
  const data = res.data

  if (res.loading && !data) return <Loading />
  if (res.error && !data) {
    return (
      <div className="page">
        <h2 className="page-title">Money flow</h2>
        <div className="card error-box">
          <div className="card-title">The money-flow data could not be loaded</div>
          <p className="muted small">{res.error}</p>
        </div>
      </div>
    )
  }
  if (!data) return null

  const available = WINDOWS.filter((k) => data.sessions[k]?.available)
  const unavailable = WINDOWS.filter((k) => !data.sessions[k]?.available)
  const w: WindowKey = available.includes(chosen) ? chosen : available[0] ?? chosen
  const span = data.sessions[w]
  const market = data.market[w]
  const top = data.top[w]
  const cov = data.coverage

  return (
    <div className="page bx mf">
      <div className="row space-between wrap">
        <div>
          <h2 className="page-title">
            Money flow <span className="muted">·</span> <Fa>جریان پول حقیقی و حقوقی</Fa>
          </h2>
          <div className="muted small">
            Where individuals and institutions traded against each other on the Tehran market, by TSETMC sector, over the
            shares whose flow is stored — measured against TSETMC's own list of who traded. Descriptions of settled
            sessions — not forecasts, and not money entering the market.
          </div>
        </div>
      </div>

      <BourseAgeNotice age={data.data_age} calendar={calendar} />

      {!cov.market_wide ? (
        <>
          <NotIngested data={data} calendar={calendar} />
          <Explainer checks={data.checks} notes={data.notes} />
        </>
      ) : (
        <>
          {cov.newest ? (
            <p className="muted small" data-testid="mf-coverage">
              Newest session, {formatDate(cov.newest_session, calendar)}: flow for {formatGrouped(cov.newest.with_flow)} of
              the {countOf(cov.newest.day_file_traded_shares, 'share')} TSETMC's day file shows trading (
              {pctText(cov.newest.share_pct)}, and {pctText(cov.newest.value_pct)} of their traded value).
            </p>
          ) : null}
          {cov.thin_dates_after_newest > 0 ? (
            <div className="callout callout-warn" data-testid="mf-thin-after" role="status">
              {formatGrouped(cov.thin_dates_after_newest)} stored date(s) after {formatDate(cov.newest_session, calendar)}{' '}
              are not market sessions and are in no figure here
              {cov.newest_thin?.date
                ? ` — the newest, ${formatDate(cov.newest_thin.date, calendar)}: ${cov.newest_thin.reason ?? 'not covered'}`
                : ''}
              . Every figure ends at {formatDate(cov.newest_session, calendar)}.
            </div>
          ) : null}

          <div className="row wrap bx-filter" role="group" aria-label="Window">
            <span className="field-label">Window</span>
            <div className="chip-row" data-testid="mf-window-chips">
              {WINDOWS.map((k) => (
                <button
                  key={k}
                  className={`chip ${w === k ? 'active' : ''}`}
                  aria-pressed={w === k}
                  disabled={!data.sessions[k]?.available}
                  title={data.sessions[k]?.reason}
                  onClick={() => setChosen(k)}
                >
                  {windowLabel(k)}
                </button>
              ))}
            </div>
            {span?.available ? (
              <span className="muted small" data-testid="mf-window-span">
                Market sessions {formatDate(span.from, calendar)} → {formatDate(span.to, calendar)}
                {span.previous_from
                  ? `; compared with ${formatDate(span.previous_from, calendar)} → ${formatDate(span.previous_to, calendar)}`
                  : '; no earlier window of this length is stored'}
                .
              </span>
            ) : null}
            {/* A disabled button can be neither focused nor hovered on every
                device, so its reason is stated as text, not only as a title. */}
            {unavailable.length > 0 ? (
              <span className="muted small" data-testid="mf-window-unavailable">
                {unavailable.map((k) => `${windowLabel(k)}: ${data.sessions[k]?.reason ?? 'not available'}`).join(' · ')}
              </span>
            ) : null}
          </div>

          {market ? <MarketTiles m={market} window={w} coverage={span?.coverage} /> : null}

          <SectorTable data={data} window={w} calendar={calendar} />

          <div className="mf-top">
            <TopList
              title={`Largest net individual inflow — ${windowLabel(w)}`}
              items={top?.inflow ?? []}
              testId="mf-top-inflow"
              empty="No company had net individual inflow in this window."
            />
            <TopList
              title={`Largest net individual outflow — ${windowLabel(w)}`}
              items={top?.outflow ?? []}
              testId="mf-top-outflow"
              empty="No company had net individual outflow in this window."
            />
          </div>
          <p className="muted small">
            Companies, every board summed and shown by its main-board symbol. Only shares on this deployment's roster link to
            a share page.
          </p>

          <Heatmap data={data} calendar={calendar} />

          <Explainer checks={data.checks} notes={data.notes} />
        </>
      )}
    </div>
  )
}
